"""Tests for V5 delete/move permissions."""

import asyncio
from pathlib import Path

from ai.messages import ToolCall
from agent.approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
)
from agent.permissions import PermissionDecisionType
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.delete import DeleteTool
from coding_agent.tools.move import MoveTool


def _run(coro):
    return asyncio.run(coro)


class _FakeApprovalHandler(ApprovalHandler):
    def __init__(self, decision: ApprovalDecision):
        self._decision = decision

    async def request(self, request):
        return self._decision


def _delete_tool(tmp_path: Path) -> DeleteTool:
    return DeleteTool(
        Workspace(tmp_path),
        FileMutationQueue(),
        CheckpointStore(),
    )


def _move_tool(tmp_path: Path) -> MoveTool:
    return MoveTool(
        Workspace(tmp_path),
        FileMutationQueue(),
        CheckpointStore(),
    )


def _version_dict(path: Path) -> dict:
    return version_of_file(path).model_dump()


def test_delete_ask(tmp_path: Path):
    (tmp_path / "a.py").write_text("x")

    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            _delete_tool(tmp_path),
            {
                "path": "a.py",
                "expected_version": _version_dict(tmp_path / "a.py"),
            },
        )
    )

    assert decision.decision == PermissionDecisionType.ASK
    assert decision.rule_id == "delete_file_ask"


def test_move_ask(tmp_path: Path):
    (tmp_path / "a.py").write_text("x")

    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            _move_tool(tmp_path),
            {
                "source": "a.py",
                "destination": "b.py",
                "expected_source_version": _version_dict(
                    tmp_path / "a.py"
                ),
            },
        )
    )

    assert decision.decision == PermissionDecisionType.ASK
    assert decision.rule_id == "move_file_ask"


def test_delete_not_shadowed_by_write_fs(tmp_path: Path):
    """delete_file 必须优先于 write_fs 命中。"""
    tool = _delete_tool(tmp_path)

    assert "write_fs" in tool.capabilities
    assert "delete_file" in tool.capabilities

    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            tool,
            {"path": "a.py", "expected_version": {}},
        )
    )

    assert decision.decision == PermissionDecisionType.ASK


def test_move_not_shadowed_by_write_fs(tmp_path: Path):
    tool = _move_tool(tmp_path)

    assert "write_fs" in tool.capabilities
    assert "move_file" in tool.capabilities

    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            tool,
            {"source": "a.py", "destination": "b.py"},
        )
    )

    assert decision.decision == PermissionDecisionType.ASK


def test_delete_deny_zero_side_effect(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")

    tool = _delete_tool(tmp_path)

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([tool]),
        permission_engine=CodingPermissionEngine(),
        approval_handler=_FakeApprovalHandler(
            ApprovalDecision.DENY
        ),
    )

    result = _run(
        dispatcher.dispatch(
            ToolCall(
                id="call_1",
                name="delete",
                arguments={
                    "path": "a.py",
                    "expected_version": _version_dict(
                        tmp_path / "a.py"
                    ),
                },
            )
        )
    )

    assert result.is_error is True
    # 文件仍存在且字节未变。
    assert (tmp_path / "a.py").read_text() == "x = 1\n"


def test_move_deny_zero_side_effect(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")

    tool = _move_tool(tmp_path)

    dispatcher = ToolDispatcher(
        registry=ToolRegistry([tool]),
        permission_engine=CodingPermissionEngine(),
        approval_handler=_FakeApprovalHandler(
            ApprovalDecision.DENY
        ),
    )

    result = _run(
        dispatcher.dispatch(
            ToolCall(
                id="call_2",
                name="move",
                arguments={
                    "source": "a.py",
                    "destination": "b.py",
                    "expected_source_version": _version_dict(
                        tmp_path / "a.py"
                    ),
                },
            )
        )
    )

    assert result.is_error is True
    # source 仍存在，destination 不存在，字节未变。
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert not (tmp_path / "b.py").exists()
