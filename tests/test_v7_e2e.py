"""V7 Stop Rule L: full FakeModel plan/evidence/edit/verify/replan flow."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage
from ai.models import Model
from ai.tools import ToolDefinition
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.agents.editor import Editor
from coding_agent.agents.planner import Planner
from coding_agent.agents.verifier import Verifier
from coding_agent.agents.viewer import Viewer
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.orchestration import CodingOrchestrator
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import default_shell_config
from coding_agent.repository_map import RepositoryMapBuilder
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import PermissionMode
from coding_agent.session import CodingSession
from coding_agent.session_store import SessionStore
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.presets import pytest_verifier


class _FakeModelProvider:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = [json.dumps(item) for item in responses]
        self.calls: list[dict] = []

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        self.calls.append({"model": model, "context": context, "tools": tools})
        yield StreamStart()
        yield StreamEnd(message=AssistantMessage(content=self.responses.pop(0)))


def _plan(*, files: list[str], queries: list[str]) -> dict:
    return {
        "objective": "Migrate internal calculate_total calls to compute_total",
        "constraints": ["Do not modify public_api.py", "Keep compatibility"],
        "steps": [
            {
                "id": "migrate",
                "objective": "Rename implementation and update internal callers",
                "target_files": files,
                "evidence_queries": queries,
                "acceptance_criteria": ["Targeted callers use compute_total"],
            }
        ],
        "acceptance_criteria": ["All fixture tests pass"],
        "revision": 1,
    }


def _responses() -> list[dict]:
    return [
        _plan(
            files=["src/calculator.py", "src/service.py"],
            queries=["calculate_total"],
        ),
        {
            "operations": [
                {
                    "kind": "edit_file",
                    "path": "src/calculator.py",
                    "replacements": [
                        {
                            "old_text": "def calculate_total(values):",
                            "new_text": "def compute_total(values):",
                        }
                    ],
                },
                {
                    "kind": "edit_file",
                    "path": "src/service.py",
                    "replacements": [
                        {
                            "old_text": "from src.calculator import calculate_total",
                            "new_text": "from src.calculator import compute_total",
                        },
                        {
                            "old_text": "return calculate_total(values)",
                            "new_text": "return compute_total(values)",
                        },
                    ],
                },
            ]
        },
        _plan(
            files=["src/calculator.py", "src/secondary.py"],
            queries=["calculate_total", "compute_total"],
        ),
        {
            "operations": [
                {
                    "kind": "edit_file",
                    "path": "src/calculator.py",
                    "replacements": [
                        {
                            "old_text": "def compute_total(values):\n    return sum(values)",
                            "new_text": (
                                "def compute_total(values):\n"
                                "    return sum(values)\n\n\n"
                                "def calculate_total(values):\n"
                                "    return compute_total(values)"
                            ),
                        }
                    ],
                },
                {
                    "kind": "edit_file",
                    "path": "src/secondary.py",
                    "replacements": [
                        {
                            "old_text": "from src.calculator import calculate_total",
                            "new_text": "from src.calculator import compute_total",
                        },
                        {
                            "old_text": "return calculate_total(values)",
                            "new_text": "return compute_total(values)",
                        },
                    ],
                },
            ]
        },
    ]


def _make_fixture(root: Path) -> bytes:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "calculator.py").write_text(
        "def calculate_total(values):\n    return sum(values)\n",
        encoding="utf-8",
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
        "def test_all_entry_points():\n"
        "    assert invoice_total([1, 2]) == 3\n"
        "    assert report_total([2, 3]) == 5\n"
        "    assert calculate_total([3, 4]) == 7\n",
        encoding="utf-8",
    )
    return wrapper.read_bytes()


@pytest.mark.asyncio
async def test_fake_model_e2e_verification_failure_drives_replan(tmp_path: Path):
    wrapper_before = _make_fixture(tmp_path)
    workspace = Workspace(tmp_path)
    provider = _FakeModelProvider(_responses())
    model = Model(id="one-shared-model", provider="fake")
    planner = Planner(provider, model)
    editor = Editor(provider, model)
    mutation_runtime = WorkspaceMutationRuntime(
        workspace,
        FileMutationQueue(),
        CheckpointStore(),
    )
    verifier = Verifier(
        VerificationPipeline(
            [
                pytest_verifier(
                    ProcessRunner(),
                    default_shell_config(),
                    command="uv run python -m pytest -q",
                    timeout_seconds=60,
                )
            ]
        )
    )
    session = CodingSession.create(
        session_id="fake-e2e",
        user_goal="Migrate the API without modifying public_api.py",
        provider=provider,
        model=model,
        tool_dispatcher=ToolDispatcher(
            ToolRegistry([]),
            CodingPermissionEngine(mode=PermissionMode.ACCEPT_EDITS),
        ),
        session_store=SessionStore(tmp_path.parent / f"{tmp_path.name}-sessions"),
        context_manager=ContextManager(
            budget=ContextBudget(max_tokens=100_000, reserve_tokens=1_000)
        ),
    )
    orchestrator = CodingOrchestrator(
        workspace=workspace,
        planner=planner,
        viewer=Viewer(workspace, observation_store=session.observations),
        editor=editor,
        mutation_runtime=mutation_runtime,
        verifier=verifier,
        repository_map_builder=RepositoryMapBuilder(workspace),
    )

    outcome = await orchestrator.run_task(session.state.user_goal, session)

    assert outcome.passed is True
    assert outcome.plan.revision == 2
    assert outcome.metrics.model_calls == 4
    assert outcome.metrics.verification_attempts == 2
    assert outcome.metrics.replan_count == 1
    assert len(provider.calls) == 4
    assert all(call["model"] is model for call in provider.calls)
    assert all(call["tools"] == [] for call in provider.calls)
    assert (tmp_path / "public_api.py").read_bytes() == wrapper_before
    assert "def compute_total" in (
        tmp_path / "src" / "calculator.py"
    ).read_text(encoding="utf-8")
    assert "import compute_total" in (
        tmp_path / "src" / "secondary.py"
    ).read_text(encoding="utf-8")
