"""Tests for execute-capability permission and approval side effects."""

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

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.powershell import PowerShellTool


def _run(coro):
    return asyncio.run(coro)


class _FakeApprovalHandler(ApprovalHandler):
    """记录请求并返回预设决定。"""

    def __init__(self, decision: ApprovalDecision):
        self._decision = decision
        self.requests: list[ApprovalRequest] = []

    async def request(
        self,
        request: ApprovalRequest,
    ) -> ApprovalDecision:
        self.requests.append(request)
        return self._decision


def _dispatcher(
    tmp_path: Path,
    decision: ApprovalDecision,
) -> ToolDispatcher:
    tool = PowerShellTool(
        workspace=Workspace(tmp_path),
        process_runner=ProcessRunner(),
    )

    return ToolDispatcher(
        registry=ToolRegistry([tool]),
        permission_engine=CodingPermissionEngine(),
        approval_handler=_FakeApprovalHandler(decision),
    )


def test_safe_execute_permission_is_allow(tmp_path: Path):
    """V9 default 模式允许 AST 证明安全的 shell 命令。"""
    tool = PowerShellTool(
        workspace=Workspace(tmp_path),
        process_runner=ProcessRunner(),
    )

    engine = CodingPermissionEngine()

    decision = _run(
        engine.evaluate(
            tool,
            {"command": "Write-Output 'hi'"},
        )
    )

    assert decision.decision == PermissionDecisionType.ALLOW
    assert decision.risk == "read_only"


def test_powershell_approval_allow_executes(tmp_path: Path):
    dispatcher = _dispatcher(
        tmp_path,
        ApprovalDecision.APPROVE,
    )

    marker = tmp_path / "marker.txt"

    result = _run(
        dispatcher.dispatch(
            ToolCall(
                id="call_1",
                name="powershell",
                arguments={
                    "command": (
                        f"Set-Content -Path '{marker}' "
                        "-Value executed"
                    )
                },
            )
        )
    )

    assert result.is_error is False
    assert marker.exists()
    assert marker.read_text().strip() == "executed"


def test_powershell_approval_deny_has_no_side_effect(tmp_path: Path):
    dispatcher = _dispatcher(
        tmp_path,
        ApprovalDecision.DENY,
    )

    marker = tmp_path / "marker.txt"

    result = _run(
        dispatcher.dispatch(
            ToolCall(
                id="call_2",
                name="powershell",
                arguments={
                    "command": (
                        f"Set-Content -Path '{marker}' "
                        "-Value executed"
                    )
                },
            )
        )
    )

    assert result.is_error is True
    assert not marker.exists()
