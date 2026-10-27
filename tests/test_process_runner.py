"""Tests for ProcessRunner bounded output / timeout / cancellation."""

import asyncio
import os
from pathlib import Path

import pytest

from coding_agent.process.runner import (
    OutputAccumulator,
    ProcessRunner,
)
from coding_agent.process.shell import powershell_config


def _run(coro):
    return asyncio.run(coro)


def _ps_command(body: str) -> str:
    """返回在 PowerShell 中执行 body 的命令字符串。"""
    return body


# ---------------------------------------------------------------------------
# OutputAccumulator：head + tail 语义
# ---------------------------------------------------------------------------


def test_accumulator_keeps_full_output_when_under_limit():
    acc = OutputAccumulator(max_bytes=100)

    acc.feed(b"hello")
    acc.feed(b" ")
    acc.feed(b"world")

    text, truncated = acc.result()

    assert text == "hello world"
    assert truncated is False


def test_accumulator_truncates_with_head_and_tail():
    acc = OutputAccumulator(max_bytes=10)

    acc.feed(b"0123456789ABCDEF")  # 16 bytes > 10

    text, truncated = acc.result()

    assert truncated is True

    # head = 前 5 字节，tail = 后 5 字节
    assert text.startswith("01234")
    assert text.endswith("BCDEF")
    assert "[output truncated]" in text


def test_accumulator_keeps_only_tail_after_truncation():
    acc = OutputAccumulator(max_bytes=10)

    acc.feed(b"ABCDEFGHIJ")  # 恰好 10 字节，尚未截断
    acc.feed(b"KLMNOP")      # 16 字节 > 10 → 截断

    text, truncated = acc.result()

    assert truncated is True
    assert text.startswith("ABCDE")
    assert text.endswith("LMNOP")


# ---------------------------------------------------------------------------
# ProcessRunner：基本语义
# ---------------------------------------------------------------------------


def test_process_runner_stdout(tmp_path: Path):
    result = _run(
        ProcessRunner().run(
            'Write-Output "MINI_PI_STDOUT"',
            cwd=tmp_path,
            shell_config=powershell_config(),
        )
    )

    assert result.exit_code == 0
    assert "MINI_PI_STDOUT" in result.stdout
    assert result.timed_out is False


def test_process_runner_stderr(tmp_path: Path):
    result = _run(
        ProcessRunner().run(
            '[Console]::Error.WriteLine("MINI_PI_STDERR")',
            cwd=tmp_path,
            shell_config=powershell_config(),
        )
    )

    assert "MINI_PI_STDERR" in result.stderr


def test_process_runner_nonzero_exit_code(tmp_path: Path):
    """pytest 失败（exit 1）必须是正常返回，不是异常。"""
    result = _run(
        ProcessRunner().run(
            "exit 1",
            cwd=tmp_path,
            shell_config=powershell_config(),
        )
    )

    assert result.exit_code == 1
    assert result.timed_out is False


def test_process_runner_cwd(tmp_path: Path):
    subdir = tmp_path / "src"
    subdir.mkdir()

    result = _run(
        ProcessRunner().run(
            "(Get-Location).Path",
            cwd=subdir,
            shell_config=powershell_config(),
        )
    )

    assert result.exit_code == 0
    assert (
        str(subdir.resolve()).lower()
        in result.stdout.strip().lower()
    )


# ---------------------------------------------------------------------------
# ProcessRunner：timeout / 截断
# ---------------------------------------------------------------------------


def test_process_runner_timeout(tmp_path: Path):
    result = _run(
        ProcessRunner().run(
            "Start-Sleep -Seconds 60",
            cwd=tmp_path,
            shell_config=powershell_config(),
            timeout_seconds=1,
        )
    )

    assert result.timed_out is True


def test_process_runner_large_output_is_truncated(tmp_path: Path):
    """进程输出远大于 max_output_bytes，内存不被全部输出撑爆。"""
    result = _run(
        ProcessRunner().run(
            "1..5000 | ForEach-Object { 'x' * 100 }",
            cwd=tmp_path,
            shell_config=powershell_config(),
            max_output_bytes=10_000,
        )
    )

    assert result.truncated is True
    assert "[output truncated]" in result.stdout

    # head + tail + marker，总量接近 10 KB 而不是 500 KB。
    assert len(result.stdout.encode("utf-8")) <= 10_000 + 1024


# ---------------------------------------------------------------------------
# ProcessRunner：cancellation
# ---------------------------------------------------------------------------


def test_process_runner_cancel_terminates_process(tmp_path: Path):
    """external cancellation 必须终止进程树并重新抛出 CancelledError。"""

    runner = ProcessRunner()

    async def scenario():
        task = asyncio.create_task(
            runner.run(
                "Start-Sleep -Seconds 60",
                cwd=tmp_path,
                shell_config=powershell_config(),
                timeout_seconds=60,
            )
        )

        # 等待进程真正启动。
        await asyncio.sleep(0.5)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ProcessRunner：进程树终止（timeout 后无 orphan child）
# ---------------------------------------------------------------------------


def test_process_runner_timeout_kills_child_process_tree(tmp_path: Path):
    """timeout 后，shell 启动的子进程也必须消失。

    命令用 PowerShell 启动一个 Python 子进程，
    记录其 PID，然后父 shell 长时间睡眠触发 timeout。

    timeout 后若只杀了 shell 主进程，Python 子进程会存活，
    我们通过重新查询该 PID 判断其是否还存在。
    """
    child_pid_file = tmp_path / "child_pid.txt"

    # 子进程：sleep 足够久，若未被杀死则一直存活。
    command = (
        "$p = Start-Process -FilePath python "
        "-ArgumentList '-c','import time; time.sleep(120)' "
        "-PassThru; "
        f"Set-Content -Path '{child_pid_file}' "
        "-Value $p.Id; "
        "Start-Sleep -Seconds 120"
    )

    result = _run(
        ProcessRunner().run(
            command,
            cwd=tmp_path,
            shell_config=powershell_config(),
            timeout_seconds=3,
        )
    )

    assert result.timed_out is True

    child_pid = int(
        child_pid_file.read_text().strip()
    )

    # 子进程应该已经消失（Windows 下 PID 不存在）。
    assert not _pid_exists(child_pid)


def _pid_exists(pid: int) -> bool:
    """Windows 下通过 tasklist 判断 PID 是否仍然存在。"""
    if os.name != "nt":
        # POSIX：kill(pid, 0) 探测。
        import signal

        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    import subprocess

    result = subprocess.run(
        [
            "tasklist",
            "/FI",
            f"PID eq {pid}",
            "/NH",
        ],
        capture_output=True,
        text=True,
    )

    return str(pid) in result.stdout
