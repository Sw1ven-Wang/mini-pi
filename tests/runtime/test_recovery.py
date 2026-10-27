import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest

from coding_agent.changes.workspace_ir import (
    CreateFileOp, EditFileOp, DeleteFileOp, MoveFileOp, WorkspaceChangeSet,
)
from coding_agent.changes.ir import TextReplacement
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.runtime.models import RecoveryRequiredError, RunStatus, RuntimeConflictError
from coding_agent.runtime.budgets import DurableBudgetTracker
from coding_agent.task_budget import TaskBudget


def setup(tmp_path, kind):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "old.txt").write_bytes(b"old\r\n")
    version = version_of_file(workspace / "old.txt")
    operations = {
        "create": CreateFileOp(path="new.txt", content="new\n"),
        "edit": EditFileOp(path="old.txt", expected_version=version,
                           replacements=[TextReplacement(old_text="old", new_text="new")]),
        "delete": DeleteFileOp(path="old.txt", expected_version=version),
        "move": MoveFileOp(source="old.txt", destination="moved.txt", expected_source_version=version),
    }
    return workspace, WorkspaceChangeSet(operations=[operations[kind]])


def crash(workspace, state_root, point, changes):
    result = subprocess.run([sys.executable, "-m", "tests.runtime.crash_worker", str(workspace),
        str(state_root), point, changes.model_dump_json()], capture_output=True, text=True, timeout=20)
    assert result.returncode == 86, result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["create", "edit", "delete", "move"])
@pytest.mark.parametrize("point", ["after_journal_prepare", "before_operation_effect",
                                    "after_operation_effect", "after_operation_commit"])
def test_real_process_crash_recovers_without_replaying(tmp_path, kind, point):
    workspace, changes = setup(tmp_path, kind)
    state_root = tmp_path / "state"
    crash(workspace, state_root, point, changes)
    kernel = RuntimeKernel(Workspace(workspace), state_root=state_root)
    try:
        kernel.mark_interrupted()
        run = kernel.store.runs()[0]
        execution = kernel.resume(run.run_id)
        if point in {"after_journal_prepare", "before_operation_effect"}:
            assert execution.run.cursor == "proposal"
            assert (workspace / "old.txt").read_bytes() == b"old\r\n"
            assert not (workspace / "new.txt").exists()
        else:
            assert execution.run.cursor == "verify"
            before = {path.name: path.read_bytes() for path in workspace.iterdir()}
            assert execution.journal().reconcile() == "applied"
            assert {path.name: path.read_bytes() for path in workspace.iterdir()} == before
    finally:
        kernel.close()


def test_partial_batch_compensation_preserves_exact_bytes(tmp_path):
    workspace, changes = setup(tmp_path, "edit")
    changes.operations.append(CreateFileOp(path="second.txt", content="second"))
    root = tmp_path / "state"
    crash(workspace, root, "after_operation_effect", changes)
    assert (workspace / "old.txt").read_bytes() == b"new\r\n"
    kernel = RuntimeKernel(Workspace(workspace), state_root=root)
    try:
        kernel.mark_interrupted()
        execution = kernel.resume(kernel.store.runs()[0].run_id)
        assert execution.run.proposal_id is None
        assert (workspace / "old.txt").read_bytes() == b"old\r\n"
        assert not (workspace / "second.txt").exists()
    finally:
        kernel.close()


def test_external_edit_is_not_overwritten(tmp_path):
    workspace, changes = setup(tmp_path, "edit")
    root = tmp_path / "state"
    crash(workspace, root, "after_operation_effect", changes)
    (workspace / "old.txt").write_bytes(b"human work")
    kernel = RuntimeKernel(Workspace(workspace), state_root=root)
    try:
        kernel.mark_interrupted()
        run = kernel.store.runs()[0]
        with pytest.raises(RecoveryRequiredError):
            kernel.resume(run.run_id)
        assert kernel.snapshot(run.run_id).status == RunStatus.NEEDS_RECONCILIATION
        assert (workspace / "old.txt").read_bytes() == b"human work"
    finally:
        kernel.close()


def test_private_state_cannot_be_inside_workspace(tmp_path):
    with pytest.raises(RuntimeConflictError):
        RuntimeKernel(Workspace(tmp_path), state_root=tmp_path / "private")


def test_budget_does_not_reset_after_restart(tmp_path):
    workspace, _ = setup(tmp_path, "edit")
    root = tmp_path / "state"
    kernel = RuntimeKernel(Workspace(workspace), state_root=root)
    execution = kernel.start("s", command="test")
    budget = DurableBudgetTracker(TaskBudget(max_model_calls=1), kernel.store, execution.run_id)
    budget.consume_model_call()
    kernel.close()
    kernel = RuntimeKernel(Workspace(workspace), state_root=root)
    try:
        budget = DurableBudgetTracker(TaskBudget(max_model_calls=1), kernel.store, execution.run_id)
        assert budget.model_calls == 1
        from coding_agent.errors import BudgetExhaustedError
        with pytest.raises(BudgetExhaustedError):
            budget.consume_model_call()
    finally:
        kernel.close()


def test_crash_during_compensation_can_itself_recover(tmp_path):
    workspace, changes = setup(tmp_path, "edit")
    changes.operations.append(CreateFileOp(path="second.txt", content="second"))
    root = tmp_path / "state"
    crash(workspace, root, "after_operation_effect", changes)
    result = subprocess.run([sys.executable, "-m", "tests.runtime.recovery_crash_worker", str(workspace), str(root)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 87, result.stdout + result.stderr
    kernel = RuntimeKernel(Workspace(workspace), state_root=root)
    try:
        kernel.mark_interrupted()
        execution = kernel.resume(kernel.store.runs()[0].run_id)
        assert execution.run.proposal_id is None
        assert (workspace / "old.txt").read_bytes() == b"old\r\n"
        assert not (workspace / "second.txt").exists()
    finally:
        kernel.close()
