"""V7 Stop Rule K: phase state machine remains outside AgentLoop."""

import asyncio
from pathlib import Path

import pytest

from ai.context import Context
from ai.models import Model
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.agents.models import (
    EvidenceBundle,
    PlanStep,
    TaskPlan,
    VerificationDecision,
)
from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.models import WorkspaceMutationResult
from coding_agent.orchestration import CodingOrchestrator, OrchestrationPhase
from coding_agent.observability.event_bus import EventBus, InMemoryEventSink
from coding_agent.observability.events import AgentEventType
from coding_agent.repository_map import RepositoryMap
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import PermissionMode
from coding_agent.errors import ApprovalRequiredError
from coding_agent.session import CodingSession, SessionStatus
from coding_agent.session_store import SessionStore


def _run(coro):
    return asyncio.run(coro)


def _plan(name: str = "new.py") -> TaskPlan:
    return TaskPlan(
        objective="Create file",
        steps=[
            PlanStep(
                id="create",
                objective="Create requested file",
                target_files=[name],
                acceptance_criteria=["File is created"],
            )
        ],
        acceptance_criteria=["Verification passes"],
    )


class _Planner:
    def __init__(self, shared, model, plans):
        self.provider = shared
        self.model = model
        self.plans = list(plans)
        self.calls = 0
        self.contexts = []

    async def create_plan(self, *, goal, context):
        self.calls += 1
        self.contexts.append(context)
        return self.plans.pop(0)


class _Viewer:
    def __init__(self, bundles):
        self.bundles = list(bundles)

    async def gather(self, plan, *, repository_notes=None):
        return self.bundles.pop(0)


class _Editor:
    def __init__(self, shared, model):
        self.provider = shared
        self.model = model
        self.calls = 0

    async def create_change_set(self, *, request, diagnostics=None):
        self.calls += 1
        return WorkspaceChangeSet(
            operations=[CreateFileOp(path=request.intent.target_files[0], content="x=1\n")]
        )


class _Mutation:
    def __init__(self):
        self.calls = 0

    async def apply(self, change_set):
        self.calls += 1
        return WorkspaceMutationResult(mutations=[], changed_files=[])


class _Verifier:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def verify(self, **kwargs):
        return self.decisions.pop(0)


class _MapBuilder:
    def build(self):
        return RepositoryMap()


def _session(
    tmp_path: Path,
    mode: PermissionMode = PermissionMode.ACCEPT_EDITS,
) -> CodingSession:
    return CodingSession.create(
        session_id="v7-session",
        user_goal="create file",
        provider=object(),
        model=Model(id="unused", provider="fake"),
        tool_dispatcher=ToolDispatcher(
            ToolRegistry([]), CodingPermissionEngine(mode=mode),
        ),
        session_store=SessionStore(tmp_path / "sessions"),
        context_manager=ContextManager(
            budget=ContextBudget(max_tokens=100_000, reserve_tokens=1_000)
        ),
    )


def _orchestrator(tmp_path, *, plans, bundles, decisions):
    shared = object()
    model = Model(id="shared", provider="fake")
    planner = _Planner(shared, model, plans)
    editor = _Editor(shared, model)
    mutation = _Mutation()
    orchestrator = CodingOrchestrator(
        workspace=Workspace(tmp_path),
        planner=planner,
        viewer=_Viewer(bundles),
        editor=editor,
        mutation_runtime=mutation,
        verifier=_Verifier(decisions),
        repository_map_builder=_MapBuilder(),
    )
    return orchestrator, planner, editor, mutation


def _decision(passed: bool) -> VerificationDecision:
    return VerificationDecision(
        passed=passed,
        failed_criteria=[] if passed else ["Verification passes"],
        should_replan=not passed,
        reason="pass" if passed else "acceptance failed",
    )


def test_orchestrator_runs_explicit_phase_state_machine(tmp_path: Path):
    orchestrator, planner, editor, mutation = _orchestrator(
        tmp_path,
        plans=[_plan()],
        bundles=[EvidenceBundle()],
        decisions=[_decision(True)],
    )

    outcome = _run(orchestrator.run_task("create file", _session(tmp_path)))

    assert outcome.trace == [
        OrchestrationPhase.RECEIVE_TASK,
        OrchestrationPhase.PLAN,
        OrchestrationPhase.GATHER_EVIDENCE,
        OrchestrationPhase.EDIT,
        OrchestrationPhase.MUTATE,
        OrchestrationPhase.VERIFY,
        OrchestrationPhase.FINAL,
    ]
    assert planner.calls == editor.calls == mutation.calls == 1
    assert outcome.metrics.task_success is True


def test_default_mode_stops_unapproved_host_mutation(tmp_path: Path):
    orchestrator, _, _, mutation = _orchestrator(
        tmp_path,
        plans=[_plan()],
        bundles=[EvidenceBundle()],
        decisions=[_decision(True)],
    )
    with pytest.raises(ApprovalRequiredError):
        _run(orchestrator.run_task(
            "create file", _session(tmp_path, PermissionMode.DEFAULT),
        ))
    assert mutation.calls == 0


def test_verification_failure_replans_and_increments_revision(tmp_path: Path):
    orchestrator, planner, editor, mutation = _orchestrator(
        tmp_path,
        plans=[_plan(), _plan()],
        bundles=[EvidenceBundle(), EvidenceBundle()],
        decisions=[_decision(False), _decision(True)],
    )
    session = _session(tmp_path)

    outcome = _run(orchestrator.run_task("create file", session))

    assert outcome.plan.revision == 2
    assert outcome.metrics.replan_count == 1
    assert outcome.metrics.verification_attempts == 2
    assert planner.calls == editor.calls == mutation.calls == 2
    assert OrchestrationPhase.REPLAN in outcome.trace
    assert session.state.status == SessionStatus.COMPLETED
    assert session.state.current_plan.revision == 2
    assert "acceptance_failure" in planner.contexts[1].session_summary


def test_insufficient_evidence_replans_before_edit(tmp_path: Path):
    orchestrator, planner, editor, mutation = _orchestrator(
        tmp_path,
        plans=[_plan(), _plan()],
        bundles=[
            EvidenceBundle(unresolved_questions=["missing evidence"]),
            EvidenceBundle(),
        ],
        decisions=[_decision(True)],
    )

    outcome = _run(orchestrator.run_task("create file", _session(tmp_path)))

    assert outcome.metrics.replan_count == 1
    assert planner.calls == 2
    assert editor.calls == mutation.calls == 1


def test_orchestrator_rejects_different_model_instances(tmp_path: Path):
    shared = object()
    with pytest.raises(ValueError, match="Model"):
        CodingOrchestrator(
            workspace=Workspace(tmp_path),
            planner=_Planner(shared, Model(id="one", provider="fake"), [_plan()]),
            viewer=_Viewer([]),
            editor=_Editor(shared, Model(id="one", provider="fake")),
            mutation_runtime=_Mutation(),
            verifier=_Verifier([]),
            repository_map_builder=_MapBuilder(),
        )


def test_orchestrator_emits_major_runtime_events(tmp_path: Path):
    orchestrator, _, _, _ = _orchestrator(
        tmp_path,
        plans=[_plan()],
        bundles=[EvidenceBundle()],
        decisions=[_decision(True)],
    )
    sink = InMemoryEventSink()
    orchestrator.event_bus = EventBus([sink])

    _run(orchestrator.run_task("create file", _session(tmp_path)))

    event_types = [event.type for event in sink.events]
    assert event_types[0] == AgentEventType.TASK_STARTED
    assert AgentEventType.PLAN_CREATED in event_types
    assert AgentEventType.MUTATION_PLANNED in event_types
    assert AgentEventType.MUTATION_APPLIED in event_types
    assert AgentEventType.VERIFICATION_STARTED in event_types
    assert AgentEventType.VERIFICATION_COMPLETED in event_types
    assert event_types[-1] == AgentEventType.TASK_COMPLETED
    assert len({event.trace_id for event in sink.events}) == 1
