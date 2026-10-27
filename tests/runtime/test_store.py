"""V11 foundation: durable state, migration guards and fail-closed races."""

import json
import sqlite3

import pytest

from coding_agent.runtime.models import (
    RunSnapshot, RunStatus, RuntimeConflictError, RuntimePersistenceError,
)
from coding_agent.runtime.store import RuntimeStore
from coding_agent.runtime.transitions import TRANSITIONS, validate_transition
from coding_agent.session import SessionSnapshot


@pytest.fixture
def store(tmp_path):
    value = RuntimeStore(tmp_path / "private")
    yield value
    value.close()


@pytest.mark.parametrize("before", list(RunStatus))
@pytest.mark.parametrize("after", list(RunStatus))
def test_all_state_transitions(before, after):
    if before == after or after in TRANSITIONS[before]:
        validate_transition(before, after)
    else:
        with pytest.raises(RuntimeConflictError):
            validate_transition(before, after)


def test_restart_and_revision(tmp_path):
    root = tmp_path / "state"
    store = RuntimeStore(root)
    run = store.create_run(RunSnapshot(session_id="s", workspace_id="w"))
    run = store.update(run.run_id, run.revision, status=RunStatus.RUNNING)
    store.close()
    store = RuntimeStore(root)
    try:
        assert store.get_run(run.run_id) == run
        with pytest.raises(RuntimeConflictError):
            store.update(run.run_id, 0, status=RunStatus.CANCELLED)
        assert [item["sequence"] for item in store.events(run.run_id)] == [1, 2]
    finally:
        store.close()


def test_second_writer_fails(store):
    with pytest.raises(RuntimePersistenceError):
        RuntimeStore(store.root)


def test_unknown_schema_not_rewritten(tmp_path):
    root = tmp_path / "state"
    store = RuntimeStore(root)
    store.close()
    with sqlite3.connect(root / "runtime.db") as db:
        db.execute("PRAGMA user_version=100")
    with pytest.raises(RuntimePersistenceError):
        RuntimeStore(root)
    with sqlite3.connect(root / "runtime.db") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 100


def test_atomic_state_and_outbox_on_failure(store):
    run = store.create_run(RunSnapshot(session_id="s", workspace_id="w"))
    store.db.execute("CREATE TRIGGER refuse_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(sqlite3.Error):
        store.update(run.run_id, run.revision, status=RunStatus.RUNNING)
    assert store.get_run(run.run_id) == run
    assert len(store.events(run.run_id)) == 1


def test_blob_integrity_and_containment(store):
    data = b"original bytes \x00\xff\r\n"
    key = store.write_blob(data)
    assert store.read_blob(key) == data
    for invalid in ("../runtime.db", "x" * 64, "A" * 64):
        with pytest.raises(RuntimePersistenceError):
            store.read_blob(invalid)
    (store.blob_root / key).write_bytes(b"corrupt")
    with pytest.raises(RuntimePersistenceError):
        store.read_blob(key)


def test_observable_events_are_redacted(store):
    run = store.create_run(RunSnapshot(session_id="s", workspace_id="w"))
    store.update(run.run_id, run.revision, payload={"api_key": "private", "reason": "Bearer abcd123456"})
    assert "private" not in json.dumps(store.events(run.run_id))
    assert "abcd123456" not in json.dumps(store.events(run.run_id))


def test_old_session_defaults_and_future_version_rejected():
    payload = {"state": {"session_id": "old", "user_goal": "goal"}, "context": {"system_prompt": "", "messages": []}}
    assert SessionSnapshot.model_validate(payload).format_version == 1
    assert SessionSnapshot.model_validate({**payload, "format_version": 2}).format_version == 2
    with pytest.raises(ValueError):
        SessionSnapshot.model_validate({**payload, "format_version": 3})


def test_reparse_detection_does_not_depend_on_path_is_junction():
    import stat
    from types import SimpleNamespace
    from coding_agent.runtime.privacy import is_link
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
    assert is_link(path)
