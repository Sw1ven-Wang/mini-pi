from pathlib import Path

from ai.messages import ToolCall
from agent.approvals import ApprovalDecision
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.errors import CommandPolicyBlockedError
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessResult
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.powershell import PowerShellTool


class _Runner:
    def __init__(self):
        self.calls = 0

    async def run(self, command, **kwargs):
        self.calls += 1
        return ProcessResult(command=command, exit_code=0, stdout="ok", stderr="")


class _Approve:
    async def request(self, request):
        return ApprovalDecision.APPROVE


async def test_pytest_allowed_or_asked(tmp_path: Path):
    runner = _Runner()
    result = await PowerShellTool(Workspace(tmp_path), runner).execute(
        "call", {"command": "uv run pytest -q"}
    )
    assert result.details["command_policy"]["decision"] in {"allow", "ask"}
    assert runner.calls == 1


async def test_remove_item_denied_with_zero_side_effect(tmp_path: Path):
    target = tmp_path / "legacy.py"
    target.write_text("keep", encoding="utf-8")
    runner = _Runner()
    tool = PowerShellTool(Workspace(tmp_path), runner)
    try:
        await tool.execute("call", {"command": "Remove-Item legacy.py"})
        raise AssertionError("expected policy denial")
    except CommandPolicyBlockedError as exc:
        assert exc.suggested_action == "Use DeleteTool instead."
    assert runner.calls == 0
    assert target.read_text(encoding="utf-8") == "keep"


async def test_move_item_suggests_move_tool(tmp_path: Path):
    tool = PowerShellTool(Workspace(tmp_path), _Runner())
    try:
        await tool.execute("call", {"command": "Move-Item a.py b.py"})
        raise AssertionError("expected policy denial")
    except CommandPolicyBlockedError as exc:
        assert exc.suggested_action == "Use MoveTool instead."


async def test_outer_approval_cannot_override_guard(tmp_path: Path):
    target = tmp_path / "legacy.py"
    target.write_text("keep", encoding="utf-8")
    runner = _Runner()
    dispatcher = ToolDispatcher(
        ToolRegistry([PowerShellTool(Workspace(tmp_path), runner)]),
        CodingPermissionEngine(),
        approval_handler=_Approve(),
    )
    result = await dispatcher.dispatch(
        ToolCall(
            id="call",
            name="powershell",
            arguments={"command": "Remove-Item legacy.py"},
        )
    )
    assert result.is_error is True
    assert "command_policy_blocked" in result.content
    assert runner.calls == 0
    assert target.exists()
