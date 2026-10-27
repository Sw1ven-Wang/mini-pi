"""Host invariants: persisted waits, migration, budgets and no false completion."""

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from agent.approvals import ApprovalDecision, ApprovalRequest
from ai.context import Context
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.runtime.budgets import DurableBudgetTracker
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.runtime.models import RunSnapshot, RunStatus, RuntimeConflictError, now
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.session import SessionSnapshot, SessionState, SessionStatus
from coding_agent.task_budget import TaskBudget


@pytest.fixture
def execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kernel = RuntimeKernel(Workspace(workspace), state_root=tmp_path / "state")
    execution = kernel.start("s", command="test", goal="goal")
    execution.permission_engine = CodingPermissionEngine()
    yield execution
    kernel.close()


def proposal(execution):
    change_set = WorkspaceChangeSet(operations=[CreateFileOp(path="new.txt", content="private source sentinel\n")])
    execution.prepare_proposal(change_set)
    request = ApprovalRequest(tool_name="workspace_mutation", arguments={}, reason="write", rule_id="ask")
    return change_set, execution.approvals.request(request)


def test_approved_once_is_bound_and_consumed(execution):
    changes, approval = proposal(execution)
    execution.approvals.resolve(approval.approval_id, ApprovalDecision.APPROVE_ONCE)
    with pytest.raises(RuntimeConflictError):
        execution.approvals.resolve(approval.approval_id, ApprovalDecision.APPROVE_ONCE)
    engine = execution.permission_engine
    execution = execution.kernel.resume(execution.run_id)
    execution.permission_engine = engine
    execution.before_apply(changes, SimpleNamespace(journal=None))
    assert execution.store.get("approval", approval.approval_id)["status"] == "consumed"
    assert "private source sentinel" not in json.dumps(execution.store.events(execution.run_id))


def test_expired_approval_is_replaced_not_executed(execution):
    _, approval = proposal(execution)
    approval.expires_at = now() - timedelta(seconds=1)
    execution.store.put("approval", approval.approval_id, approval.model_dump(mode="json"), execution.run_id)
    with pytest.raises(RuntimeConflictError):
        execution.approvals.resolve(approval.approval_id, ApprovalDecision.APPROVE_ONCE)
    replacement = execution.approvals.request(ApprovalRequest(tool_name="workspace_mutation", arguments={}, reason="write", rule_id="ask"))
    assert replacement.approval_id != approval.approval_id
    assert execution.store.get("approval", approval.approval_id)["status"] == "expired"


def test_approval_cannot_authorize_another_run(execution):
    _, approval = proposal(execution)
    from coding_agent.runtime.kernel import RuntimeExecution
    other = execution.store.create_run(RunSnapshot(session_id="other", workspace_id=execution.kernel.workspace_id))
    with pytest.raises(RuntimeConflictError):
        RuntimeExecution(execution.kernel, other.run_id).approvals.resolve(approval.approval_id, ApprovalDecision.APPROVE_ONCE)


def test_legacy_import_is_idempotent_and_keeps_reconciliation_flag(execution):
    store = execution.kernel.session_store
    snapshot = SessionSnapshot(state=SessionState(session_id="legacy", user_goal="old", status=SessionStatus.ACTIVE), context=Context(system_prompt="", messages=[]))
    first = store.import_legacy(snapshot)
    snapshot.state.user_goal = "must not overwrite"
    second = store.import_legacy(snapshot)
    assert first == second
    assert first.format_version == 2
    assert first.state.metadata["v11_import"]["requires_reconciliation"]


@pytest.mark.parametrize("budget", [{"model_calls": -1}, {"turns": 0.5}, {"active_seconds": float("nan")}, {"unknown_counter": 1}])
def test_corrupt_budget_fails_closed(budget):
    with pytest.raises(ValueError):
        RunSnapshot(session_id="s", workspace_id="w", budget=budget)


def test_budget_limits_cannot_silently_increase(execution):
    DurableBudgetTracker(TaskBudget(max_model_calls=1), execution.store, execution.run_id)
    with pytest.raises(RuntimeConflictError):
        DurableBudgetTracker(TaskBudget(max_model_calls=20), execution.store, execution.run_id)


def test_unfinished_operations_cannot_be_accepted(execution):
    proposal(execution)
    with pytest.raises(RuntimeConflictError, match="unfinished"):
        execution.succeed(SimpleNamespace(model_dump=lambda **_: {}))


def test_interrupted_verifier_cannot_be_replayed_or_release_workspace(execution):
    execution.update(cursor="verification_started")
    execution.kernel.mark_interrupted()
    from coding_agent.runtime.models import RecoveryRequiredError
    with pytest.raises(RecoveryRequiredError):
        execution.kernel.resume(execution.run_id)
    with pytest.raises(RecoveryRequiredError):
        execution.kernel.cancel(execution.run_id)
    assert execution.run.status == RunStatus.NEEDS_RECONCILIATION


async def test_journal_failure_prevents_file_effect(execution):
    changes, _ = proposal(execution)
    runtime = WorkspaceMutationRuntime(execution.workspace, FileMutationQueue(), CheckpointStore(), journal=execution.journal())
    execution.store.db.execute("CREATE TRIGGER no_journal BEFORE UPDATE ON values_store WHEN NEW.kind='operation' BEGIN SELECT RAISE(ABORT,'disk full'); END")
    # prepare is idempotent, but the APPLYING commit fails before the file write.
    import sqlite3
    with pytest.raises(sqlite3.Error):
        await runtime.apply(changes)
    assert not (execution.workspace.root / "new.txt").exists()


def test_start_persists_initial_session_and_cancel_projection(execution):
    snapshot = execution.kernel.session_store.load("s")
    assert snapshot.state.user_goal == "goal"
    execution.kernel.cancel(execution.run_id)
    assert execution.kernel.session_store.load("s").state.status == SessionStatus.CANCELLED


def test_consumed_once_can_continue_only_its_same_unstarted_operation(execution):
    changes, approval = proposal(execution)
    execution.approvals.resolve(approval.approval_id, ApprovalDecision.APPROVE_ONCE)
    engine = execution.permission_engine
    execution = execution.kernel.resume(execution.run_id)
    execution.permission_engine = engine
    execution.before_apply(changes, SimpleNamespace(journal=None))
    execution.kernel.mark_interrupted()
    execution = execution.kernel.resume(execution.run_id)
    execution.permission_engine = engine
    recovered = execution.approvals.request(ApprovalRequest(tool_name="workspace_mutation", arguments={}, reason="write", rule_id="ask"))
    assert recovered.approval_id == approval.approval_id
    assert recovered.status.value == "approved"
    assert execution.store.get("approval", approval.approval_id)["status"] == "consumed"


async def test_kernel_rejects_concurrent_executors_and_closing_active_store(execution):
    import asyncio
    blocker = asyncio.Event()

    async def run_task(*args):
        await blocker.wait()

    workflow = SimpleNamespace(runtime_execution=execution, run_task=run_task)
    session = SimpleNamespace(state=SimpleNamespace(user_goal="goal"))
    task = asyncio.create_task(execution.kernel.execute(execution, workflow, session))
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeConflictError):
            await execution.kernel.execute(execution, workflow, session)
        with pytest.raises(RuntimeConflictError):
            execution.kernel.close()
        with pytest.raises(RuntimeConflictError):
            execution.kernel.resume(execution.run_id)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert execution.run.status == RunStatus.INTERRUPTED


def test_intent_cursor_and_operation_rows_commit_atomically(execution):
    import sqlite3
    execution.store.db.execute("CREATE TRIGGER no_intent BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'disk full'); END")
    with pytest.raises(sqlite3.Error):
        execution.prepare_proposal(WorkspaceChangeSet(operations=[CreateFileOp(path="new.txt", content="x")]))
    assert execution.run.proposal_id is None
    assert execution.store.values("operation", execution.run_id) == []
    assert not (execution.workspace.root / "new.txt").exists()
