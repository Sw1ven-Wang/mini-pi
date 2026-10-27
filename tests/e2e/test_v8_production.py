import json
from pathlib import Path

import pytest

from ai.context import Context
from ai.models import Model
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.agents.models import EvidenceBundle, PlanStep, TaskPlan, VerificationDecision
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import CreateFileOp, EditFileOp, TextReplacement, WorkspaceChangeSet
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.errors import CommandPolicyBlockedError
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.observability.artifacts import ArtifactStore
from coding_agent.observability.audit import AuditSink
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.observability.runtime import MutationEventObserver
from coding_agent.observability.tracing import TraceContext
from coding_agent.orchestration import CodingOrchestrator
from coding_agent.process.runner import ProcessRunner
from coding_agent.replay.recorder import TraceRecorder
from coding_agent.repository_map import RepositoryMap
from coding_agent.sandbox.local import LocalSandbox
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import PermissionMode
from coding_agent.session import CodingSession
from coding_agent.session_store import SessionStore
from coding_agent.testing.fault_injection import FaultInjectedError, ScriptedFaultInjector
from coding_agent.tools.powershell import PowerShellTool


class _Planner:
    def __init__(self, provider, model):
        self.provider = provider
        self.model = model

    async def create_plan(self, **kwargs):
        return TaskPlan(
            objective="Create then repair",
            steps=[PlanStep(
                id="write-config", objective="Produce accepted value",
                target_files=["result.txt"], acceptance_criteria=["value is final"],
            )],
            acceptance_criteria=["value is final"],
        )


class _Editor:
    def __init__(self, provider, model, workspace):
        self.provider = provider
        self.model = model
        self.workspace = workspace
        self.calls = 0

    async def create_change_set(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return WorkspaceChangeSet(operations=[
                CreateFileOp(path="result.txt", content="intermediate\n")
            ])
        target = self.workspace.resolve("result.txt")
        return WorkspaceChangeSet(operations=[EditFileOp(
            path="result.txt", expected_version=version_of_file(target),
            replacements=[TextReplacement(old_text="intermediate", new_text="final")],
        )])


class _Viewer:
    budget_tracker = None

    async def gather(self, *args, **kwargs):
        return EvidenceBundle()


class _Verifier:
    def __init__(self):
        self.calls = 0

    async def verify(self, **kwargs):
        self.calls += 1
        passed = self.calls == 2
        return VerificationDecision(
            passed=passed,
            failed_criteria=[] if passed else ["value is final"],
            should_replan=not passed,
            reason="passed" if passed else "repair required",
        )


class _Map:
    def build(self):
        return RepositoryMap()


def _session(tmp_path):
    return CodingSession.create(
        session_id="v8-e2e", user_goal="create final result",
        provider=object(), model=Model(id="session", provider="fake"),
        tool_dispatcher=ToolDispatcher(
            ToolRegistry([]),
            CodingPermissionEngine(mode=PermissionMode.ACCEPT_EDITS),
        ),
        session_store=SessionStore(tmp_path / ".sessions"),
        context_manager=ContextManager(
            budget=ContextBudget(max_tokens=100_000, reserve_tokens=1_000)
        ),
    )


async def test_full_trace_links_plan_replan_mutations_versions_and_verification(tmp_path: Path):
    workspace = Workspace(tmp_path)
    shared = object()
    model = Model(id="same-model", provider="fake")
    recorder = TraceRecorder()
    audit = AuditSink(tmp_path / ".mini_pi" / "audit")
    orchestrator = CodingOrchestrator(
        workspace=workspace,
        planner=_Planner(shared, model), viewer=_Viewer(),
        editor=_Editor(shared, model, workspace),
        mutation_runtime=WorkspaceMutationRuntime(
            workspace, FileMutationQueue(), CheckpointStore()
        ),
        verifier=_Verifier(), repository_map_builder=_Map(),
        event_bus=EventBus([recorder, audit]), trace_recorder=recorder,
    )
    outcome = await orchestrator.run_task("create final result", _session(tmp_path))
    record = recorder.finalize()
    types = [event.type for event in record.events]
    assert outcome.passed and (tmp_path / "result.txt").read_text().strip() == "final"
    assert AgentEventType.PLAN_CREATED in types
    assert AgentEventType.PLAN_REVISED in types
    assert types.count(AgentEventType.MUTATION_APPLIED) == 2
    assert types.count(AgentEventType.VERIFICATION_COMPLETED) == 2
    assert types[-1] == AgentEventType.TASK_COMPLETED
    mutation_events = [event for event in record.events if event.type == AgentEventType.MUTATION_APPLIED]
    assert mutation_events[1].payload["versions"][0]["before"]
    assert mutation_events[1].payload["versions"][0]["after"]
    planned = [event for event in record.events if event.type == AgentEventType.MUTATION_PLANNED]
    assert planned[0].payload["reason_step_ids"] == ["write-config"]
    assert len(record.tool_actions) == 2
    assert record.verification_summary["passed"] is True


async def test_dangerous_command_is_denied_before_process_and_file_unchanged(tmp_path: Path):
    target = tmp_path / "legacy.py"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(CommandPolicyBlockedError) as caught:
        await PowerShellTool(Workspace(tmp_path), ProcessRunner()).execute(
            "danger", {"command": "Remove-Item legacy.py"}
        )
    assert caught.value.suggested_action == "Use DeleteTool instead."
    assert target.read_text(encoding="utf-8") == "keep"


async def test_secret_is_absent_from_sandbox_trace_and_artifact(monkeypatch, tmp_path: Path):
    secret = "SECRET_E2E_VALUE"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    sandbox = LocalSandbox()
    session = await sandbox.start(workspace=Workspace(tmp_path))
    try:
        process = await session.run("Get-ChildItem Env:")
    finally:
        await session.stop()
    recorder = TraceRecorder()
    await recorder.emit(AgentEvent(
        session_id="s", trace_id="t", span_id="p",
        type=AgentEventType.TASK_STARTED, payload={"api_key": secret},
    ))
    artifact = ArtifactStore(tmp_path / "artifacts").put_text(process.stdout, kind="env")
    recorder.add_artifact(artifact)
    serialized = recorder.finalize(trace_id="t", session_id="s").model_dump_json()
    assert secret not in process.stdout
    assert secret not in serialized
    assert secret not in json.dumps(artifact.model_dump(mode="json"))


async def test_fault_recovery_is_traced_and_workspace_returns_to_original(tmp_path: Path):
    trace = TraceContext.root()
    recorder = TraceRecorder()
    runtime = WorkspaceMutationRuntime(
        Workspace(tmp_path), FileMutationQueue(), CheckpointStore(),
        ScriptedFaultInjector({"before_second_file_write": 1}),
        MutationEventObserver(event_bus=EventBus([recorder]), session_id="s", trace=trace),
    )
    with pytest.raises(FaultInjectedError):
        await runtime.apply(WorkspaceChangeSet(operations=[
            CreateFileOp(path="one.txt", content="one"),
            CreateFileOp(path="two.txt", content="two"),
        ]))
    types = [event.type for event in recorder.events]
    assert types == [AgentEventType.ROLLBACK_STARTED, AgentEventType.ROLLBACK_COMPLETED]
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
