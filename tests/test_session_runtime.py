"""V6 CodingSession lifecycle, persistence hook and resume safety tests."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from ai.models import Model
from ai.tools import ToolDefinition
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from agent.tools import AgentTool, AgentToolResult
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import ObservationStore
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.session import (
    CodingSession,
    INTERRUPTED_SESSION_PROMPT,
    SessionPersistenceObserver,
    SessionSnapshot,
    SessionState,
    SessionStatus,
)
from coding_agent.session_store import SessionStore
from coding_agent.errors import SessionReconciliationRequiredError
from coding_agent.tools.edit import EditTool
from coding_agent.tools.read import ReadTool


class _FakeProvider:
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

        if not self.responses:
            raise RuntimeError("no response")

        yield StreamEnd(message=self.responses.pop(0))


class _CountingMutationTool(AgentTool):
    name = "delete"
    description = "count only"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, tool_call_id: str, arguments: dict):
        self.executions += 1
        return AgentToolResult(content="executed")


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(id="fake", provider="fake")


def _manager() -> ContextManager:
    return ContextManager(
        budget=ContextBudget(
            max_tokens=100_000,
            reserve_tokens=1_000,
        )
    )


def _dispatcher(root: Path, *extra_tools: AgentTool) -> ToolDispatcher:
    return ToolDispatcher(
        ToolRegistry([ReadTool(Workspace(root)), *extra_tools]),
        CodingPermissionEngine(),
    )


def test_coding_session_run_persists_context_and_observations(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    provider = _FakeProvider(
        [
            AssistantMessage(
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name="read",
                        arguments={"path": "app.py"},
                    )
                ]
            ),
            AssistantMessage(content="done"),
        ]
    )
    store = SessionStore(tmp_path / ".mini_pi" / "sessions")
    session = CodingSession.create(
        session_id="session-1",
        user_goal="inspect app.py",
        provider=provider,
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
        system_prompt="base",
    )

    result = _run(session.run("inspect app.py"))
    loaded = store.load("session-1")

    assert result.content == "done"
    assert loaded.state.status == SessionStatus.COMPLETED
    assert loaded.state.turn_count == 2
    assert loaded.state.interrupted is False
    assert len(loaded.context.messages) == 4
    assert loaded.state.observations[0].path == "app.py"
    assert loaded.state.observations[0].exists is True


def test_resume_restores_context_and_observations_without_disk_refresh(
    tmp_path: Path,
):
    file_path = tmp_path / "app.py"
    file_path.write_text("old", encoding="utf-8")
    store = SessionStore(tmp_path / ".mini_pi" / "sessions")
    first = CodingSession.create(
        session_id="session-1",
        user_goal="inspect app.py",
        provider=_FakeProvider(
            [
                AssistantMessage(
                    tool_calls=[
                        ToolCall(
                            id="read-1",
                            name="read",
                            arguments={"path": "app.py"},
                        )
                    ]
                ),
                AssistantMessage(content="first done"),
            ]
        ),
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )
    _run(first.run("inspect app.py"))
    saved_context = store.load("session-1").context
    observed_digest = first.observations.get("app.py").version.digest  # type: ignore[union-attr]

    file_path.write_text("external", encoding="utf-8")
    resumed = CodingSession.resume(
        "session-1",
        provider=_FakeProvider([AssistantMessage(content="resumed")]),
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )

    assert resumed.context == saved_context
    assert resumed.observations.get("app.py").version.digest == observed_digest  # type: ignore[union-attr]
    assert resumed.session_id == first.session_id


def test_session_persistence_observer_marks_pending_group_interrupted(
    tmp_path: Path,
):
    store = SessionStore(tmp_path / "sessions")
    state = SessionState(session_id="session-1", user_goal="goal")
    context = Context(
        messages=[
            UserMessage(content="goal"),
            AssistantMessage(
                tool_calls=[
                    ToolCall(id="call-1", name="read", arguments={})
                ]
            ),
        ]
    )
    observer = SessionPersistenceObserver(
        state=state,
        session_store=store,
        observations=ObservationStore(),
        context_manager=_manager(),
    )

    _run(observer.on_context_updated(context, turn_index=1))

    assert store.load("session-1").state.interrupted is True

    context.messages.append(
        ToolResultMessage(
            tool_call_id="call-1",
            name="read",
            content="ok",
        )
    )
    _run(observer.on_context_updated(context, turn_index=1))

    assert store.load("session-1").state.interrupted is False


def test_resume_orphan_tool_call_does_not_reexecute_mutation(tmp_path: Path):
    (tmp_path / "old.py").write_text("inspect me", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    store.save(
        SessionSnapshot(
            state=SessionState(
                session_id="session-1",
                user_goal="delete old.py",
            ),
            context=Context(
                system_prompt="base",
                messages=[
                    UserMessage(content="delete old.py"),
                    AssistantMessage(
                        tool_calls=[
                            ToolCall(
                                id="dangerous",
                                name="delete",
                                arguments={"path": "old.py"},
                            )
                        ]
                    ),
                ],
            ),
        )
    )
    tool = _CountingMutationTool()
    session = CodingSession.resume(
        "session-1",
        provider=_FakeProvider([AssistantMessage(content="reconciled")]),
        model=_model(),
        tool_dispatcher=ToolDispatcher(
            ToolRegistry([tool, ReadTool(Workspace(tmp_path))]),
            CodingPermissionEngine(),
        ),
        session_store=store,
        context_manager=_manager(),
    )

    assert session.state.interrupted is True
    assert len(session.context.messages) == 1
    assert INTERRUPTED_SESSION_PROMPT in session.context.system_prompt

    with pytest.raises(SessionReconciliationRequiredError):
        _run(session.run("continue without inspecting"))

    assert tool.executions == 0
    assert store.load("session-1").state.interrupted is True

    session.provider = _FakeProvider(
        [
            AssistantMessage(
                tool_calls=[
                    ToolCall(
                        id="inspect",
                        name="read",
                        arguments={"path": "old.py"},
                    )
                ]
            ),
            AssistantMessage(content="reconciled"),
        ]
    )
    _run(session.run("inspect current state now"))

    assert tool.executions == 0
    assert store.load("session-1").state.interrupted is False


def test_coding_session_binds_version_resolver(tmp_path: Path):
    workspace = Workspace(tmp_path)
    edit = EditTool(
        workspace,
        FileMutationQueue(),
        CheckpointStore(),
    )
    dispatcher = _dispatcher(tmp_path, edit)

    CodingSession.create(
        session_id="session-1",
        user_goal="edit app.py",
        provider=_FakeProvider([AssistantMessage(content="done")]),
        model=_model(),
        tool_dispatcher=dispatcher,
        session_store=SessionStore(tmp_path / "sessions"),
        context_manager=_manager(),
    )

    assert edit.version_resolver is not None
    assert dispatcher.result_observer is not None


def test_agent_failure_marks_session_failed(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    session = CodingSession.create(
        session_id="session-1",
        user_goal="goal",
        provider=_FakeProvider([]),
        model=_model(),
        tool_dispatcher=_dispatcher(tmp_path),
        session_store=store,
        context_manager=_manager(),
    )

    with pytest.raises(RuntimeError, match="no response"):
        _run(session.run("goal"))

    assert store.load("session-1").state.status == SessionStatus.FAILED
