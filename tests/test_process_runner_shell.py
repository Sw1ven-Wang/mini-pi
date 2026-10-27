"""Tests proving ProcessRunner actually runs the configured shell."""

import asyncio
from pathlib import Path

from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import (
    bash_config,
    powershell_config,
)


def _run(coro):
    return asyncio.run(coro)


def test_process_runner_explicit_powershell(tmp_path: Path):
    """显式 powershell_config 时，Runtime 真的是 PowerShell。"""
    result = _run(
        ProcessRunner().run(
            (
                'Write-Output "MINI_PI"; '
                "$PSVersionTable.PSVersion.Major"
            ),
            cwd=tmp_path,
            shell_config=powershell_config(),
        )
    )

    assert result.exit_code == 0
    assert "MINI_PI" in result.stdout
    assert "7" in result.stdout or "5" in result.stdout


def test_process_runner_default_is_powershell_on_windows(
    tmp_path: Path,
):
    """不传 shell_config 时，Windows 默认应为 PowerShell。"""
    import os

    if os.name != "nt":
        import pytest

        pytest.skip("仅 Windows 需要此断言")

    result = _run(
        ProcessRunner().run(
            (
                'Write-Output "DEFAULT_SHELL"; '
                "$PSVersionTable.PSVersion.Major"
            ),
            cwd=tmp_path,
        )
    )

    assert result.exit_code == 0
    assert "DEFAULT_SHELL" in result.stdout
