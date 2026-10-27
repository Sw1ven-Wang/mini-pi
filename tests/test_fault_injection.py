from pathlib import Path

import pytest

from ai.context import Context
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.errors import SessionPersistenceError, WorkspaceRecoveryError
from coding_agent.filesystem.atomic_write import atomic_write_bytes
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.session import SessionSnapshot, SessionState
from coding_agent.session_store import SessionStore
from coding_agent.testing.fault_injection import FaultInjectedError, ScriptedFaultInjector


def test_atomic_fault_preserves_original(tmp_path: Path):
    target = tmp_path / "value.txt"
    target.write_bytes(b"original")
    injector = ScriptedFaultInjector({"before_atomic_replace": 1})
    with pytest.raises(FaultInjectedError):
        atomic_write_bytes(target, b"changed", fault_injector=injector)
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob(".value.txt.*"))


async def test_multifile_failure_recovers(tmp_path: Path):
    injector = ScriptedFaultInjector({"before_second_file_write": 1})
    runtime = WorkspaceMutationRuntime(
        Workspace(tmp_path), FileMutationQueue(), CheckpointStore(), injector
    )
    with pytest.raises(FaultInjectedError):
        await runtime.apply(WorkspaceChangeSet(operations=[
            CreateFileOp(path="one.txt", content="one"),
            CreateFileOp(path="two.txt", content="two"),
        ]))
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()


async def test_recovery_failure_is_structured(tmp_path: Path):
    injector = ScriptedFaultInjector({
        "before_second_file_write": 1,
        "rollback_failure": 1,
    })
    runtime = WorkspaceMutationRuntime(
        Workspace(tmp_path), FileMutationQueue(), CheckpointStore(), injector
    )
    with pytest.raises(WorkspaceRecoveryError) as caught:
        await runtime.apply(WorkspaceChangeSet(operations=[
            CreateFileOp(path="one.txt", content="one"),
            CreateFileOp(path="two.txt", content="two"),
        ]))
    assert caught.value.details["recovery_report"]["failures"]


def test_session_save_fault_leaves_no_partial_json(tmp_path: Path):
    store = SessionStore(
        tmp_path, ScriptedFaultInjector({"session_save_failure": 1})
    )
    snapshot = SessionSnapshot(
        state=SessionState(session_id="safe-session", user_goal="test"),
        context=Context(system_prompt="system"),
    )
    with pytest.raises(SessionPersistenceError):
        store.save(snapshot)
    assert not (tmp_path / "safe-session" / "session.json").exists()
