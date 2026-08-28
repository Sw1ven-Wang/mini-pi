import asyncio

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.tools.powershell import PowerShellTool


def test_powershell_tool_really_uses_powershell(
    tmp_path,
):
    tool = PowerShellTool(
        workspace=Workspace(tmp_path),
        process_runner=ProcessRunner(),
    )

    result = asyncio.run(
        tool.execute(
            "call_1",
            {
                "command": (
                    'Write-Output "MINI_PI"; '
                    '$PSVersionTable.PSVersion.Major'
                )
            },
        )
    )

    assert (
        result.details["exit_code"]
        == 0
    )

    assert "MINI_PI" in result.content

    assert (
        result.details["shell"]
        == "powershell"
    )
def test_powershell_tool_uses_workspace_cwd(
    tmp_path,
):
    subdir = tmp_path / "src"
    subdir.mkdir()

    tool = PowerShellTool(
        workspace=Workspace(tmp_path),
        process_runner=ProcessRunner(),
    )

    result = asyncio.run(
        tool.execute(
            "call_1",
            {
                "command": "(Get-Location).Path",
                "cwd": "src",
            },
        )
    )

    assert (
        result.details["exit_code"]
        == 0
    )

    stdout = (
        result.details["stdout"]
        .strip()
    )

    assert (
        str(subdir.resolve()).lower()
        in stdout.lower()
    )