"""Tests for V3 tool permissions (glob/search/patch)."""

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
from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.glob import GlobTool
from coding_agent.tools.patch import PatchTool
from coding_agent.tools.search import SearchTool


def _run(coro):
    return asyncio.run(coro)


class _FakeApprovalHandler(ApprovalHandler):
    def __init__(self, decision: ApprovalDecision):
        self._decision = decision
        self.requests: list[ApprovalRequest] = []

    async def request(self, request):
        self.requests.append(request)
        return self._decision


def _patch_tool(tmp_path: Path) -> PatchTool:
    return PatchTool(
        Workspace(tmp_path),
        FileMutationQueue(),
        CheckpointStore(),
    )


def test_glob_is_allow(tmp_path: Path):
    tool = GlobTool(Workspace(tmp_path))
    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(tool, {"pattern": "**/*.py"})
    )

    assert decision.decision == PermissionDecisionType.ALLOW


def test_search_is_allow(tmp_path: Path):
    tool = SearchTool(Workspace(tmp_path))
    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(tool, {"query": "foo"})
    )

    assert decision.decision == PermissionDecisionType.ALLOW


def test_patch_is_ask(tmp_path: Path):
    tool = _patch_tool(tmp_path)
    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            tool,
            {"files": [{"path": "a.py", "replacements": []}]},
        )
    )

    assert decision.decision == PermissionDecisionType.ASK


def test_multi_file_edit_priority_over_edit_file(tmp_path: Path):
    """PatchTool 同时有 edit_file 与 multi_file_edit，
    必须先命中 multi_file_edit → ASK，不能被 edit_file → ALLOW 抢占。"""

    tool = _patch_tool(tmp_path)

    # 确认工具确实同时携带两个 capability。
    assert "edit_file" in tool.capabilities
    assert "multi_file_edit" in tool.capabilities

    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            tool,
            {"files": [{"path": "a.py", "replacements": []}]},
        )
    )

    assert decision.decision == PermissionDecisionType.ASK
    assert decision.rule_id == "multi_file_edit_ask"


def test_patch_deny_zero_side_effect(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")

    tool = _patch_tool(tmp_path)

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
                name="patch",
                arguments={
                    "files": [
                        {
                            "path": "a.py",
                            "replacements": [
                                {
                                    "old_text": "foo()",
                                    "new_text": "bar()",
                                }
                            ],
                        }
                    ]
                },
            )
        )
    )

    assert result.is_error is True
    assert (tmp_path / "a.py").read_text() == "foo()\n"
