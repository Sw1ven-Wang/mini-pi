import pytest

from coding_agent.safety.command_analysis import CommandRisk
from coding_agent.safety.command_policy import (
    CommandDecisionType,
    CommandPolicy,
)


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("Get-Content file.py", CommandDecisionType.ALLOW),
        ("uv run pytest -q", CommandDecisionType.ALLOW),
        ("Set-Content file.py x", CommandDecisionType.ASK),
        ("Invoke-WebRequest https://example.com", CommandDecisionType.ASK),
        ("pip install x", CommandDecisionType.ASK),
        ("custom-command", CommandDecisionType.ASK),
        ("Remove-Item file.py", CommandDecisionType.DENY),
    ],
)
def test_policy_matrix(command, decision):
    assert CommandPolicy().evaluate(command).decision == decision


def test_build_test_can_require_approval():
    assert CommandPolicy(allow_build_test=False).evaluate("pytest -q").decision == CommandDecisionType.ASK


def test_remove_item_suggests_delete_tool():
    decision = CommandPolicy().evaluate("Remove-Item old.py")
    assert decision.risk == CommandRisk.DESTRUCTIVE
    assert decision.suggestion == "Use DeleteTool instead."


def test_move_item_suggests_move_tool():
    assert CommandPolicy().evaluate("Move-Item a.py b.py").suggestion == "Use MoveTool instead."
