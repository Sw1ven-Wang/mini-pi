from __future__ import annotations

import pytest

from coding_agent.errors import CommandPolicyBlockedError
from coding_agent.filesystem.workspace import Workspace
from coding_agent.tools.bash import BashTool
from coding_agent.safety.command_analysis import CommandRisk, analyze_command
from coding_agent.safety.command_policy import CommandDecisionType, CommandPolicy
from coding_agent.safety.shell_ir import ParserStatus, ShellKind
from coding_agent.safety.shell_parsers import BashParser, PowerShellParser


def test_powershell_ast_reports_subcommands_operators_and_redirects():
    result = PowerShellParser().parse(
        "Get-Content input.txt; Write-Output ok > output.txt"
    )
    assert result.status == ParserStatus.OK
    assert [item.executable for item in result.commands] == [
        "get-content", "write-output",
    ]
    assert ";" in result.operators
    assert [(item.operator, item.target) for item in result.redirections] == [
        (">", "output.txt"),
    ]


def test_bash_ast_unwraps_environment_and_timeout():
    result = BashParser().parse("env FOO=bar timeout 5 pytest -q")
    assert result.status == ParserStatus.OK
    assert result.commands[0].executable == "pytest"
    assert result.commands[0].wrappers == ["env", "timeout"]
    assert result.commands[0].assignments == {"FOO": "bar"}


def test_bash_ast_unwraps_absolute_wrapper_paths():
    result = BashParser().parse("/usr/bin/env /usr/bin/timeout 5 rm victim.txt")
    assert result.status == ParserStatus.OK
    assert result.commands[0].executable == "rm"
    assert result.commands[0].wrappers == ["env", "timeout"]
    decision = CommandPolicy(shell=ShellKind.BASH).evaluate(
        "/usr/bin/env /usr/bin/timeout 5 rm victim.txt"
    )
    assert decision.decision == CommandDecisionType.DENY


def test_compound_command_uses_strongest_decision_for_both_shells():
    assert analyze_command(
        "Get-Content safe.txt; Remove-Item victim.txt",
        shell=ShellKind.POWERSHELL,
    ).risk == CommandRisk.DESTRUCTIVE
    assert analyze_command(
        "cat safe.txt && rm victim.txt",
        shell=ShellKind.BASH,
    ).risk == CommandRisk.DESTRUCTIVE


_POWERSHELL_ADVERSARIAL = [
    command
    for index in range(25)
    for command in (
        f"Get-Content file{index}.txt; Remove-Item victim{index}.txt",
        f"Write-Output ok | Move-Item a{index}.txt b{index}.txt",
        f"git clean -fd path{index}",
        f"pwsh -EncodedCommand payload{index}",
        f"Custom-Tool{index} --unknown-mode",
    )
]
_BASH_ADVERSARIAL = [
    command
    for index in range(25)
    for command in (
        f"cat file{index}.txt && rm -f victim{index}.txt",
        f"env VALUE={index} timeout 5 sh -c 'echo unsafe'",
        f"git reset --hard HEAD~{index}",
        f"echo ok > output{index}.txt",
        f"custom-tool-{index} --unknown-mode",
    )
]


@pytest.mark.parametrize("command", _POWERSHELL_ADVERSARIAL)
def test_powershell_adversarial_corpus_has_zero_false_allow(command: str):
    decision = CommandPolicy(shell=ShellKind.POWERSHELL).evaluate(command)
    assert decision.decision != CommandDecisionType.ALLOW


@pytest.mark.parametrize("command", _BASH_ADVERSARIAL)
def test_bash_adversarial_corpus_has_zero_false_allow(command: str):
    decision = CommandPolicy(shell=ShellKind.BASH).evaluate(command)
    assert decision.decision != CommandDecisionType.ALLOW


def test_parser_error_is_never_allowed():
    decision = CommandPolicy(shell=ShellKind.BASH).evaluate("echo 'unterminated")
    assert decision.decision == CommandDecisionType.ASK
    assert decision.analysis.parse_result.status == ParserStatus.ERROR


def test_tampered_powershell_bridge_is_never_started(tmp_path):
    bridge = tmp_path / "bridge.ps1"
    bridge.write_text("Write-Output 'must not execute'", encoding="utf-8")
    parser = PowerShellParser()
    parser.bridge = bridge
    result = parser.parse("Get-Content app.py")
    assert result.status == ParserStatus.UNAVAILABLE
    assert result.commands == []


@pytest.mark.parametrize("parser", [PowerShellParser(), BashParser()])
def test_parser_rejects_oversized_commands_without_execution(parser):
    result = parser.parse("echo " + "x" * 100_001)
    assert result.status == ParserStatus.ERROR
    assert result.commands == []


@pytest.mark.parametrize(
    ("shell", "command"),
    [
        (ShellKind.POWERSHELL, "pytest -q 2>&1"),
        (ShellKind.BASH, "pytest -q 2>&1"),
    ],
)
def test_file_descriptor_duplication_is_not_a_file_write(shell, command):
    assert analyze_command(command, shell=shell).risk == CommandRisk.BUILD_TEST


class _NeverRunner:
    def __init__(self):
        self.calls = 0

    async def run(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("runner must not be reached")


async def test_bash_direct_execution_keeps_shared_hard_deny_guard(tmp_path):
    runner = _NeverRunner()
    tool = BashTool(Workspace(tmp_path), runner)
    with pytest.raises(CommandPolicyBlockedError):
        await tool.execute("call", {"command": "rm victim.txt"})
    assert runner.calls == 0
