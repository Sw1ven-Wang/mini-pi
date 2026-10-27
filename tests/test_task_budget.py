import asyncio

import pytest

from ai.messages import ToolCall
from agent.approvals import ApprovalDecision
from agent.permissions import PermissionDecision, PermissionDecisionType
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from agent.tools import AgentTool, AgentToolResult
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.errors import BudgetExhaustedError
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.task_budget import BudgetTracker, TaskBudget


def test_task_budget_enforces_all_counters():
    tracker = BudgetTracker(TaskBudget(
        max_turns=1, max_model_calls=1, max_tool_calls=1,
        max_replans=0, max_duration_seconds=10,
    ))
    tracker.consume_turn()
    tracker.consume_model_call()
    tracker.consume_tool_call()
    with pytest.raises(BudgetExhaustedError):
        tracker.consume_turn()
    with pytest.raises(BudgetExhaustedError):
        tracker.consume_model_call()
    with pytest.raises(BudgetExhaustedError):
        tracker.consume_tool_call()
    with pytest.raises(BudgetExhaustedError):
        tracker.consume_replan()


async def test_budget_timeout_cancels_model_request():
    cancelled = asyncio.Event()

    async def model_request():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    tracker = BudgetTracker(TaskBudget(max_duration_seconds=0.03))
    with pytest.raises(BudgetExhaustedError):
        await tracker.run(model_request())
    assert cancelled.is_set()


class _AskPermission:
    async def evaluate(self, tool, arguments):
        return PermissionDecision(
            decision=PermissionDecisionType.ASK,
            rule_id="ask",
            reason="test",
        )


class _BlockingApproval:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def request(self, request):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        return ApprovalDecision.APPROVE


class _NeverExecutedTool(AgentTool):
    name = "probe"
    description = "probe"
    parameters = {"type": "object"}
    capabilities = frozenset({"write_fs"})

    def __init__(self):
        self.executed = False

    async def execute(self, tool_call_id, arguments):
        self.executed = True
        return AgentToolResult(content="bad")


async def test_cancel_permission_wait_propagates_without_execution():
    tool = _NeverExecutedTool()
    approval = _BlockingApproval()
    dispatcher = ToolDispatcher(
        ToolRegistry([tool]), _AskPermission(), approval_handler=approval
    )
    task = asyncio.create_task(
        dispatcher.dispatch(ToolCall(id="x", name="probe", arguments={}))
    )
    await approval.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert approval.cancelled.is_set()
    assert tool.executed is False


async def test_cancel_mutation_recovers_consistent_state(tmp_path):
    runtime = WorkspaceMutationRuntime(
        Workspace(tmp_path), FileMutationQueue(), CheckpointStore()
    )
    original_apply = runtime._apply_one
    first_applied = asyncio.Event()

    async def delayed_apply(index, operation):
        if index == 1:
            first_applied.set()
            await asyncio.Event().wait()
        return await original_apply(index, operation)

    runtime._apply_one = delayed_apply
    task = asyncio.create_task(runtime.apply(WorkspaceChangeSet(operations=[
        CreateFileOp(path="one.txt", content="one"),
        CreateFileOp(path="two.txt", content="two"),
    ])))
    await first_applied.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
