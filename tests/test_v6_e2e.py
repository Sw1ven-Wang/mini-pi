"""V6 E2E: compaction, restart/resume and external-change recovery."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage, ToolCall, UserMessage
from ai.models import Model
from ai.tools import ToolDefinition
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.compaction import build_message_groups
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import PermissionMode
from coding_agent.session import CodingSession, SessionStatus
from coding_agent.session_store import SessionStore
from coding_agent.tools.edit import EditTool
from coding_agent.tools.read import ReadTool


class _ScriptedProvider:
    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[Context] = []

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        self.calls.append(context.model_copy(deep=True))
        yield StreamStart()
        yield StreamEnd(message=self.responses.pop(0))


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(id="fake", provider="fake")


def _dispatcher(root: Path) -> ToolDispatcher:
    workspace = Workspace(root)
    queue = FileMutationQueue()
    checkpoints = CheckpointStore()
    return ToolDispatcher(
        ToolRegistry(
            [
                ReadTool(workspace),
                EditTool(workspace, queue, checkpoints),
            ]
        ),
        CodingPermissionEngine(mode=PermissionMode.ACCEPT_EDITS),
    )


def _manager(*, compact: bool = False) -> ContextManager:
    if compact:
        return ContextManager(
            budget=ContextBudget(
                max_tokens=1_000,
                reserve_tokens=100,
                compact_threshold_ratio=0.60,
            )
        )

    return ContextManager(
        budget=ContextBudget(
            max_tokens=100_000,
            reserve_tokens=1_000,
        )
    )


def _read(call_id: str, path: str) -> AssistantMessage:
    return AssistantMessage(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="read",
                arguments={"path": path},
            )
        ]
    )


def _edit(
    call_id: str,
    path: str,
    old_text: str,
    new_text: str,
) -> AssistantMessage:
    return AssistantMessage(
        tool_calls=[
            ToolCall(
                id=call_id,
                name="edit",
                arguments={
                    "path": path,
                    "edits": [
                        {"old_text": old_text, "new_text": new_text}
                    ],
                },
            )
        ]
    )


def test_v6_long_task_compacts_continues_and_passes(tmp_path: Path):
    padding = "# " + ("context " * 90) + "\n"
    (tmp_path / "a.py").write_text(
        padding + "value = 1\n", encoding="utf-8"
    )
    (tmp_path / "b.py").write_text(
        padding + "value = 1\n", encoding="utf-8"
    )
    provider = _ScriptedProvider(
        [
            _read("read-a", "a.py"),
            _edit("edit-a", "a.py", "value = 1", "value = 2"),
            _read("read-b", "b.py"),
            _edit("edit-b", "b.py", "value = 1", "value = 2"),
            AssistantMessage(content="verification passed"),
        ]
    )
    store = SessionStore(tmp_path / ".mini_pi" / "sessions")
    session = CodingSession.create(
        session_id="long-task",
        user_goal="update both files and verify",
        provider=provider,
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(compact=True),
    )

    result = _run(session.run("update both files and verify"))

    assert result.content == "verification passed"
    assert session.state.compaction_count >= 1
    assert "value = 2" in (tmp_path / "a.py").read_text(encoding="utf-8")
    assert "value = 2" in (tmp_path / "b.py").read_text(encoding="utf-8")
    assert {item.path for item in session.state.observations} == {
        "a.py",
        "b.py",
    }
    assert session.state.status == SessionStatus.COMPLETED

    # Every provider request remains a valid whole-group context after compact.
    for call_context in provider.calls:
        build_message_groups(call_context.messages)
        assert "update both files and verify" in (
            call_context.system_prompt
            + "\n"
            + "\n".join(
                message.content
                for message in call_context.messages
                if isinstance(message, UserMessage)
            )
        )


def test_v6_save_restart_resume_continues_second_file(tmp_path: Path):
    (tmp_path / "a.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("value = 1\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".mini_pi" / "sessions")
    first = CodingSession.create(
        session_id="resume-task",
        user_goal="update a.py and b.py",
        provider=_ScriptedProvider(
            [
                _read("read-a", "a.py"),
                _edit("edit-a", "a.py", "1", "2"),
                AssistantMessage(content="first phase done"),
            ]
        ),
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )
    _run(first.run("update a.py first"))
    first_message_count = len(first.context.messages)

    second = CodingSession.resume(
        "resume-task",
        provider=_ScriptedProvider(
            [
                _read("read-b", "b.py"),
                _edit("edit-b", "b.py", "1", "2"),
                AssistantMessage(content="all done"),
            ]
        ),
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )

    assert second.session_id == first.session_id
    assert len(second.context.messages) == first_message_count
    assert second.observations.get("a.py") is not None

    _run(second.run("continue with b.py"))

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "value = 2\n"
    assert {item.path for item in second.state.observations} == {
        "a.py",
        "b.py",
    }
    assert second.state.status == SessionStatus.COMPLETED
    assert second.state.turn_count == 6


def test_v6_resume_external_change_stale_reread_then_pass(tmp_path: Path):
    file_path = tmp_path / "app.py"
    file_path.write_text("value = 1\n", encoding="utf-8")
    store = SessionStore(tmp_path / ".mini_pi" / "sessions")
    first = CodingSession.create(
        session_id="stale-task",
        user_goal="update app.py safely",
        provider=_ScriptedProvider(
            [
                _read("initial-read", "app.py"),
                AssistantMessage(content="paused"),
            ]
        ),
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )
    _run(first.run("read app.py"))

    # User/IDE changes the file while the process is stopped.
    file_path.write_text("value = 9\n", encoding="utf-8")
    provider = _ScriptedProvider(
        [
            _edit("stale-edit", "app.py", "value = 9", "value = 10"),
            _read("fresh-read", "app.py"),
            _edit("fresh-edit", "app.py", "value = 9", "value = 10"),
            AssistantMessage(content="safe recovery passed"),
        ]
    )
    resumed = CodingSession.resume(
        "stale-task",
        provider=provider,
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )

    result = _run(resumed.run("continue safely"))

    assert result.content == "safe recovery passed"
    assert file_path.read_text(encoding="utf-8") == "value = 10\n"

    stale_result = provider.calls[1].messages[-1]
    assert stale_result.role == "tool"
    assert stale_result.is_error is True  # type: ignore[union-attr]
    assert "stale_version" in stale_result.content

    fresh_read_result = provider.calls[2].messages[-1]
    assert fresh_read_result.role == "tool"
    assert fresh_read_result.is_error is False  # type: ignore[union-attr]
    assert "value = 9" in fresh_read_result.content
