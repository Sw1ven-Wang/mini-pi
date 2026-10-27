"""Tests for PowerShellTool: shell identity, cwd, nonzero exit, timeout, truncation."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.errors import (
    PathOutsideWorkspaceError,
    ProcessTimeoutError,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.tools.powershell import PowerShellTool


def _run(coro):
    return asyncio.run(coro)


def _tool(tmp_path: Path) -> PowerShellTool:
    return PowerShellTool(
        workspace=Workspace(tmp_path),
        process_runner=ProcessRunner(),
    )


def test_powershell_tool_really_uses_powershell(tmp_path: Path):
    tool = _tool(tmp_path)

    result = _run(
        tool.execute(
            "call_1",
            {
                "command": (
                    'Write-Output "MINI_PI"; '
                    "$PSVersionTable.PSVersion.Major"
                )
            },
        )
    )

    assert result.details["exit_code"] == 0
    assert result.details["shell"] == "powershell"
    assert "MINI_PI" in result.details["stdout"]


def test_powershell_tool_uses_workspace_cwd(tmp_path: Path):
    subdir = tmp_path / "src"
    subdir.mkdir()

    tool = _tool(tmp_path)

    result = _run(
        tool.execute(
            "call_1",
            {
                "command": "(Get-Location).Path",
                "cwd": "src",
            },
        )
    )

    assert result.details["exit_code"] == 0

    stdout = result.details["stdout"].strip()

    assert (
        str(subdir.resolve()).lower()
        in stdout.lower()
    )


def test_powershell_tool_rejects_workspace_escape(tmp_path: Path):
    tool = _tool(tmp_path)

    with pytest.raises(PathOutsideWorkspaceError):
        _run(
            tool.execute(
                "call_1",
                {
                    "command": "(Get-Location).Path",
                    "cwd": "../escape",
                },
            )
        )


def test_powershell_tool_preserves_nonzero_exit_code(tmp_path: Path):
    """pytest exit 1 不应被转成 Tool error。"""
    tool = _tool(tmp_path)

    result = _run(
        tool.execute(
            "call_1",
            {"command": "exit 1"},
        )
    )

    assert result.details["exit_code"] == 1
    assert result.details["timed_out"] is False
    assert "exit_code: 1" in result.content


def test_powershell_tool_timeout(tmp_path: Path):
    tool = _tool(tmp_path)

    with pytest.raises(ProcessTimeoutError):
        _run(
            tool.execute(
                "call_1",
                {
                    "command": "Start-Sleep -Seconds 60",
                    "timeout_seconds": 1,
                },
            )
        )


def test_powershell_tool_output_truncated(tmp_path: Path):
    tool = _tool(tmp_path)

    result = _run(
        tool.execute(
            "call_1",
            {
                "command": "1..5000 | ForEach-Object { 'x' * 100 }",
                "max_output_bytes": 10_000,
            },
        )
    )

    assert result.details["truncated"] is True
    assert "[output truncated]" in result.content
