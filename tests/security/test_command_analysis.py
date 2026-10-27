import pytest

from coding_agent.safety.command_analysis import CommandRisk, analyze_command


@pytest.mark.parametrize(
    ("command", "risk"),
    [
        ("Get-ChildItem", CommandRisk.READ_ONLY),
        ("Get-Content app.py", CommandRisk.READ_ONLY),
        ("Select-String -Path app.py -Pattern x", CommandRisk.READ_ONLY),
        ("git status", CommandRisk.READ_ONLY),
        ("git diff", CommandRisk.READ_ONLY),
        ("uv run pytest -q", CommandRisk.BUILD_TEST),
        ("ruff check .", CommandRisk.BUILD_TEST),
        ("python -m compileall src", CommandRisk.BUILD_TEST),
        ("Set-Content file.txt x", CommandRisk.WRITE),
        ("'x' | Out-File file.txt", CommandRisk.WRITE),
        ("Invoke-WebRequest https://example.com", CommandRisk.NETWORK),
        ("pip install package", CommandRisk.INSTALL),
        ("npm install", CommandRisk.INSTALL),
        ("npm test", CommandRisk.OPAQUE),
        ("unlink file.py", CommandRisk.DESTRUCTIVE),
        ("Remove-Item file.py", CommandRisk.DESTRUCTIVE),
        ("Move-Item a.py b.py", CommandRisk.DESTRUCTIVE),
        ("ri file.py", CommandRisk.DESTRUCTIVE),
        ("mi a.py b.py", CommandRisk.DESTRUCTIVE),
        ("rni a.py b.py", CommandRisk.DESTRUCTIVE),
        ("git reset --hard", CommandRisk.DESTRUCTIVE),
        ("git clean -fd", CommandRisk.DESTRUCTIVE),
        ("custom-tool --flag", CommandRisk.UNKNOWN),
    ],
)
def test_command_classification(command, risk):
    analysis = analyze_command(command)
    assert analysis.risk == risk
    assert analysis.heuristic is False
    assert analysis.parse_result is not None
    assert analysis.parse_result.status.value == "ok"


def test_compound_command_uses_most_restrictive_risk():
    assert analyze_command("git status; Remove-Item file.py").risk == CommandRisk.DESTRUCTIVE


def test_opaque_execution_is_conservatively_destructive():
    assert analyze_command("powershell -EncodedCommand AAAA").risk == CommandRisk.DESTRUCTIVE
    assert analyze_command("pwsh -enco AAAA").risk == CommandRisk.DESTRUCTIVE
    assert analyze_command("pwsh -e AAAA").risk == CommandRisk.DESTRUCTIVE


def test_dynamic_argument_never_downgrades_known_network_risk():
    assert analyze_command(
        "Invoke-WebRequest $url",
    ).risk == CommandRisk.NETWORK


@pytest.mark.parametrize(
    "command",
    [
        "git -C . reset --hard HEAD",
        "git -c core.fsmonitor=false clean -fd",
        "git push origin +main:main",
        "git push --force-with-lease=main origin main",
    ],
)
def test_git_global_options_and_force_refspecs_remain_hard_deny(command):
    analysis = analyze_command(command)
    assert analysis.risk == CommandRisk.DESTRUCTIVE
    assert any(operation.hard_deny for operation in analysis.operations)
