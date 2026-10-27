"""V6 SessionState and atomic JSON store tests."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai.context import Context
from ai.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.errors import (
    SessionNotFoundError,
    SessionPersistenceError,
)
from coding_agent.filesystem.versions import FileVersion
from coding_agent.observations import (
    ObservationSource,
    ObservedFileState,
)
from coding_agent.session import (
    SessionSnapshot,
    SessionState,
    SessionStatus,
)
from coding_agent.session_store import SessionStore


def _snapshot(session_id: str = "session-1") -> SessionSnapshot:
    return SessionSnapshot(
        state=SessionState(
            session_id=session_id,
            user_goal="fix tests",
            metadata={"branch": "main"},
        ),
        context=Context(
            system_prompt="system",
            messages=[
                UserMessage(content="fix tests"),
                AssistantMessage(
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="read",
                            arguments={"path": "app.py"},
                        )
                    ]
                ),
                ToolResultMessage(
                    tool_call_id="call-1",
                    name="read",
                    content="value = 1",
                ),
                AssistantMessage(content="done"),
            ],
        ),
    )


def test_session_save_load(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    snapshot = _snapshot()

    store.save(snapshot)
    loaded = store.load(snapshot.state.session_id)

    assert loaded == snapshot
    assert store.exists(snapshot.state.session_id)


def test_session_message_subclasses_roundtrip(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store.save(_snapshot())

    messages = store.load("session-1").context.messages

    assert isinstance(messages[0], UserMessage)
    assert isinstance(messages[1], AssistantMessage)
    assert messages[1].tool_calls[0].arguments == {"path": "app.py"}
    assert isinstance(messages[2], ToolResultMessage)
    assert messages[2].tool_call_id == "call-1"
    assert isinstance(messages[3], AssistantMessage)


def test_session_observations_roundtrip(tmp_path: Path):
    snapshot = _snapshot()
    observation = ObservedFileState(
        path="app.py",
        exists=True,
        version=FileVersion(digest="abc", size_bytes=3),
        source=ObservationSource.READ,
        observed_at=datetime.now(timezone.utc),
    )
    snapshot.state.observations = [observation]
    store = SessionStore(tmp_path / "sessions")

    store.save(snapshot)

    assert store.load("session-1").state.observations == [observation]


def test_session_atomic_save_preserves_previous_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = SessionStore(tmp_path / "sessions")
    original = _snapshot()
    store.save(original)
    changed = original.model_copy(deep=True)
    changed.state.status = SessionStatus.COMPLETED

    def fail_replace(source, destination):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr("coding_agent.session_store.os.replace", fail_replace)

    with pytest.raises(SessionPersistenceError):
        store.save(changed)

    assert store.load("session-1") == original
    assert list((tmp_path / "sessions" / "session-1").glob("*.tmp")) == []


def test_session_unknown_id(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")

    with pytest.raises(SessionNotFoundError):
        store.load("missing")


@pytest.mark.parametrize(
    "session_id",
    ["../escape", "..", r"C:\escape", "/absolute", "has/slash"],
)
def test_session_id_rejects_path_traversal(session_id: str):
    with pytest.raises((ValueError, ValidationError)):
        SessionState(session_id=session_id, user_goal="goal")


def test_session_does_not_persist_secret(tmp_path: Path):
    snapshot = _snapshot()
    snapshot.state.metadata = {
        "api_key": "sk-test-secret",
        "nested": {
            "authorization": "Bearer private",
            "safe": "kept",
        },
    }
    snapshot.context.messages[0].content = (
        "never persist sk-abcdefghijklmnop"
    )
    assistant = snapshot.context.messages[1]
    assert isinstance(assistant, AssistantMessage)
    assistant.tool_calls[0].arguments["api_key"] = "sk-toolsecret123"
    store = SessionStore(tmp_path / "sessions")

    store.save(snapshot)

    raw = (
        tmp_path / "sessions" / "session-1" / "session.json"
    ).read_text(encoding="utf-8")
    assert "sk-test-secret" not in raw
    assert "Bearer private" not in raw
    assert "sk-abcdefghijklmnop" not in raw
    assert "sk-toolsecret123" not in raw
    assert store.load("session-1").state.metadata == {
        "nested": {"safe": "kept"}
    }
    loaded = store.load("session-1")
    assert loaded.context.messages[0].content == (
        "never persist [REDACTED]"
    )
    loaded_assistant = loaded.context.messages[1]
    assert isinstance(loaded_assistant, AssistantMessage)
    assert "api_key" not in loaded_assistant.tool_calls[0].arguments


def test_session_rejects_runtime_objects():
    with pytest.raises(ValidationError):
        SessionState(
            session_id="session-1",
            user_goal="goal",
            metadata={"provider": object()},
        )
