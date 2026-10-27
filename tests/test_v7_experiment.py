"""V7 Stop Rule N: same-model V6 baseline versus phase pipeline."""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from time import perf_counter

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage, ToolCall, ToolResultMessage
from ai.models import Model
from ai.tools import ToolDefinition
from agent.approvals import ApprovalDecision
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.agents.editor import EDITOR_SYSTEM_PROMPT, Editor
from coding_agent.agents.planner import PLANNER_SYSTEM_PROMPT, Planner
from coding_agent.agents.verifier import Verifier
from coding_agent.agents.viewer import Viewer
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.context_budget import ApproxTokenEstimator, ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.experiments import compare_metrics, render_metrics_table
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.orchestration import CodingOrchestrator, OrchestrationMetrics
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import default_shell_config
from coding_agent.repository_map import RepositoryMapBuilder
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import PermissionMode
from coding_agent.session import CodingSession
from coding_agent.session_store import SessionStore
from coding_agent.tools.delete import DeleteTool
from coding_agent.tools.edit import EditTool
from coding_agent.tools.glob import GlobTool
from coding_agent.tools.move import MoveTool
from coding_agent.tools.patch import PatchTool
from coding_agent.tools.powershell import PowerShellTool
from coding_agent.tools.read import ReadTool
from coding_agent.tools.search import SearchTool
from coding_agent.tools.write import WriteTool
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.presets import pytest_verifier


class _Record:
    def __init__(self, *, phase, input_tokens, output_tokens, tool_names):
        self.phase = phase
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_names = tool_names


class _SharedFakeProvider:
    """One Provider instance used by both experiment arms."""

    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)
        self.records: list[_Record] = []
        self.estimator = ApproxTokenEstimator()

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        response = self.responses.pop(0)
        if context.system_prompt == PLANNER_SYSTEM_PROMPT:
            phase = "planner"
        elif context.system_prompt == EDITOR_SYSTEM_PROMPT:
            phase = "editor"
        else:
            phase = "baseline"
        self.records.append(
            _Record(
                phase=phase,
                input_tokens=self.estimator.estimate_text(context.model_dump_json()),
                output_tokens=self.estimator.estimate_text(response.model_dump_json()),
                tool_names=[call.name for call in response.tool_calls],
            )
        )
        yield StreamStart()
        yield StreamEnd(message=response)


class _ApproveAll:
    async def request(self, request):
        return ApprovalDecision.APPROVE


def _fixture(root: Path) -> bytes:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "calculator.py").write_text(
        "def calculate_total(values):\n    return sum(values)\n", encoding="utf-8"
    )
    (root / "src" / "service.py").write_text(
        "from src.calculator import calculate_total\n\n\n"
        "def invoice_total(values):\n    return calculate_total(values)\n",
        encoding="utf-8",
    )
    (root / "src" / "secondary.py").write_text(
        "from src.calculator import calculate_total\n\n\n"
        "def report_total(values):\n    return calculate_total(values)\n",
        encoding="utf-8",
    )
    wrapper = root / "public_api.py"
    wrapper.write_text(
        "from src.calculator import calculate_total as _calculate_total\n\n\n"
        "def calculate_total(values):\n    return _calculate_total(values)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_totals.py").write_text(
        "from public_api import calculate_total\n"
        "from src.secondary import report_total\n"
        "from src.service import invoice_total\n\n\n"
        "def test_totals():\n"
        "    assert invoice_total([1, 2]) == 3\n"
        "    assert report_total([2, 3]) == 5\n"
        "    assert calculate_total([3, 4]) == 7\n",
        encoding="utf-8",
    )
    (root / "docs" / "architecture.md").write_text("x" * 4000, encoding="utf-8")
    (root / "src" / "unrelated.py").write_text("DATA = 'x' * 2000\n", encoding="utf-8")
    (root / "settings.json").write_text('{"feature": true}\n', encoding="utf-8")
    return wrapper.read_bytes()


def _baseline_responses() -> list[AssistantMessage]:
    return [
        AssistantMessage(tool_calls=[ToolCall(id="s1", name="search", arguments={"query":"calculate_total"})]),
        AssistantMessage(
            tool_calls=[
                ToolCall(id=f"r{i}", name="read", arguments={"path": path})
                for i, path in enumerate(
                    [
                        "src/calculator.py", "src/service.py", "src/secondary.py",
                        "public_api.py", "tests/test_totals.py", "docs/architecture.md",
                        "src/unrelated.py", "settings.json",
                    ],
                    start=1,
                )
            ]
        ),
        AssistantMessage(tool_calls=[ToolCall(id="bad", name="edit", arguments={
            "path":"src/calculator.py",
            "edits":[{"old_text":"def calculate_total(items):","new_text":"def compute_total(items):"}],
        })]),
        AssistantMessage(tool_calls=[ToolCall(id="p1", name="patch", arguments={"files":[
            {"path":"src/calculator.py","replacements":[{"old_text":"def calculate_total(values):","new_text":"def compute_total(values):"}]},
            {"path":"src/service.py","replacements":[
                {"old_text":"from src.calculator import calculate_total","new_text":"from src.calculator import compute_total"},
                {"old_text":"return calculate_total(values)","new_text":"return compute_total(values)"},
            ]},
        ]})]),
        AssistantMessage(tool_calls=[ToolCall(id="v1", name="powershell", arguments={"command":"uv run python -m pytest -q","timeout_seconds":60})]),
        AssistantMessage(tool_calls=[ToolCall(id="p2", name="patch", arguments={"files":[
            {"path":"src/calculator.py","replacements":[{"old_text":"def compute_total(values):\n    return sum(values)","new_text":"def compute_total(values):\n    return sum(values)\n\n\ndef calculate_total(values):\n    return compute_total(values)"}]},
            {"path":"src/secondary.py","replacements":[
                {"old_text":"from src.calculator import calculate_total","new_text":"from src.calculator import compute_total"},
                {"old_text":"return calculate_total(values)","new_text":"return compute_total(values)"},
            ]},
        ]})]),
        AssistantMessage(tool_calls=[ToolCall(id="v2", name="powershell", arguments={"command":"uv run python -m pytest -q","timeout_seconds":60})]),
        AssistantMessage(content="done"),
    ]


def _v7_responses() -> list[AssistantMessage]:
    def plan(files, queries):
        return {
            "objective":"Migrate internal calculate_total calls to compute_total",
            "constraints":["Do not modify public_api.py","Keep compatibility"],
            "steps":[{"id":"migrate","objective":"Rename and update callers","target_files":files,"evidence_queries":queries,"acceptance_criteria":["Targeted callers migrated"]}],
            "acceptance_criteria":["All fixture tests pass"],
            "revision":1,
        }
    payloads = [
        plan(["src/calculator.py","src/service.py"],["calculate_total"]),
        {"operations":[
            {"kind":"edit_file","path":"src/calculator.py","replacements":[{"old_text":"def calculate_total(values):","new_text":"def compute_total(values):"}]},
            {"kind":"edit_file","path":"src/service.py","replacements":[
                {"old_text":"from src.calculator import calculate_total","new_text":"from src.calculator import compute_total"},
                {"old_text":"return calculate_total(values)","new_text":"return compute_total(values)"},
            ]},
        ]},
        plan(["src/calculator.py","src/secondary.py"],["calculate_total","compute_total"]),
        {"operations":[
            {"kind":"edit_file","path":"src/calculator.py","replacements":[{"old_text":"def compute_total(values):\n    return sum(values)","new_text":"def compute_total(values):\n    return sum(values)\n\n\ndef calculate_total(values):\n    return compute_total(values)"}]},
            {"kind":"edit_file","path":"src/secondary.py","replacements":[
                {"old_text":"from src.calculator import calculate_total","new_text":"from src.calculator import compute_total"},
                {"old_text":"return calculate_total(values)","new_text":"return compute_total(values)"},
            ]},
        ]},
    ]
    return [AssistantMessage(content=json.dumps(item)) for item in payloads]


def _manager():
    return ContextManager(budget=ContextBudget(max_tokens=100_000, reserve_tokens=1_000))


def _dispatcher(workspace: Workspace):
    queue = FileMutationQueue()
    checkpoints = CheckpointStore()
    return ToolDispatcher(
        ToolRegistry([
            GlobTool(workspace), SearchTool(workspace), ReadTool(workspace),
            WriteTool(workspace, queue, checkpoints), EditTool(workspace, queue, checkpoints),
            PatchTool(workspace, queue, checkpoints), MoveTool(workspace, queue, checkpoints),
            DeleteTool(workspace, queue, checkpoints), PowerShellTool(workspace, ProcessRunner()),
        ]),
        CodingPermissionEngine(),
        approval_handler=_ApproveAll(),
    )


def _metrics_from_baseline(records, session, duration_ms, root):
    tool_calls = [name for record in records for name in record.tool_names]
    error_codes = []
    for message in session.context.messages:
        if isinstance(message, ToolResultMessage) and message.is_error:
            try:
                error_codes.append(json.loads(message.content).get("code"))
            except json.JSONDecodeError:
                pass
    read_paths = {
        call.arguments["path"]
        for message in session.context.messages
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls
        if call.name == "read"
    }
    mutation_names = {"edit", "patch", "write", "delete", "move"}
    return OrchestrationMetrics(
        task_success=(
            "def compute_total" in (root / "src" / "calculator.py").read_text()
            and "import compute_total" in (root / "src" / "secondary.py").read_text()
        ),
        model_calls=len(records),
        input_tokens=sum(item.input_tokens for item in records),
        output_tokens=sum(item.output_tokens for item in records),
        editor_input_tokens=sum(
            item.input_tokens for item in records
            if mutation_names.intersection(item.tool_names)
        ),
        tool_calls=len(tool_calls),
        read_calls=tool_calls.count("read"),
        search_calls=tool_calls.count("search"),
        files_read=len(read_paths),
        mutation_attempts=sum(name in mutation_names for name in tool_calls),
        no_match_count=error_codes.count("no_match"),
        ambiguous_match_count=error_codes.count("ambiguous_match"),
        stale_version_count=error_codes.count("stale_version"),
        verification_attempts=tool_calls.count("powershell"),
        replan_count=0,
        total_duration_ms=duration_ms,
    )


async def test_baseline_vs_v7_same_model_provider_fixture_and_task(tmp_path: Path):
    baseline_root = tmp_path / "baseline"
    v7_root = tmp_path / "v7"
    baseline_root.mkdir()
    v7_root.mkdir()
    baseline_wrapper = _fixture(baseline_root)
    v7_wrapper = _fixture(v7_root)
    provider = _SharedFakeProvider(_baseline_responses())
    model = Model(id="one-shared-model", provider="fake")
    goal = "Migrate calculate_total but do not modify public_api.py"

    baseline_session = CodingSession.create(
        session_id="baseline",
        user_goal=goal,
        provider=provider,
        model=model,
        tool_dispatcher=_dispatcher(Workspace(baseline_root)),
        session_store=SessionStore(tmp_path / "baseline-sessions"),
        context_manager=_manager(),
    )
    started = perf_counter()
    await baseline_session.run(goal)
    baseline_duration = round((perf_counter() - started) * 1000)
    baseline_records = list(provider.records)
    baseline_metrics = _metrics_from_baseline(
        baseline_records, baseline_session, baseline_duration, baseline_root
    )

    provider.responses.extend(_v7_responses())
    v7_start_record = len(provider.records)
    workspace = Workspace(v7_root)
    v7_session = CodingSession.create(
        session_id="v7",
        user_goal=goal,
        provider=provider,
        model=model,
        tool_dispatcher=ToolDispatcher(
            ToolRegistry([]),
            CodingPermissionEngine(mode=PermissionMode.ACCEPT_EDITS),
        ),
        session_store=SessionStore(tmp_path / "v7-sessions"),
        context_manager=_manager(),
    )
    orchestrator = CodingOrchestrator(
        workspace=workspace,
        planner=Planner(provider, model),
        viewer=Viewer(workspace, observation_store=v7_session.observations),
        editor=Editor(provider, model),
        mutation_runtime=WorkspaceMutationRuntime(
            workspace, FileMutationQueue(), CheckpointStore()
        ),
        verifier=Verifier(VerificationPipeline([
            pytest_verifier(ProcessRunner(), default_shell_config(), command="uv run python -m pytest -q", timeout_seconds=60)
        ])),
        repository_map_builder=RepositoryMapBuilder(workspace),
    )
    outcome = await orchestrator.run_task(goal, v7_session)
    v7_records = provider.records[v7_start_record:]
    v7_metrics = outcome.metrics.model_copy(update={
        "model_calls": len(v7_records),
        "input_tokens": sum(item.input_tokens for item in v7_records),
        "output_tokens": sum(item.output_tokens for item in v7_records),
        "editor_input_tokens": sum(item.input_tokens for item in v7_records if item.phase == "editor"),
    })
    comparison = compare_metrics(baseline_metrics, v7_metrics)

    assert baseline_metrics.task_success is True
    assert v7_metrics.task_success is True
    assert (baseline_root / "public_api.py").read_bytes() == baseline_wrapper
    assert (v7_root / "public_api.py").read_bytes() == v7_wrapper
    assert all(record.phase == "baseline" for record in baseline_records)
    assert {record.phase for record in v7_records} == {"planner", "editor"}
    assert comparison.editor_context_reduction_percent > 0
    assert v7_metrics.files_read < baseline_metrics.files_read
    assert v7_metrics.mutation_attempts < baseline_metrics.mutation_attempts
    assert baseline_metrics.no_match_count == 1
    assert v7_metrics.no_match_count == 0
    assert v7_metrics.replan_count == 1

    print("\nV7_EXPERIMENT_JSON=" + comparison.model_dump_json())
    print(render_metrics_table(comparison))


def test_fixture_task_set_covers_twelve_required_scenarios():
    path = Path(__file__).parent / "fixtures" / "v7_task_set.json"
    tasks = json.loads(path.read_text(encoding="utf-8"))

    assert len(tasks) == 12
    assert len({task["category"] for task in tasks}) == 12
