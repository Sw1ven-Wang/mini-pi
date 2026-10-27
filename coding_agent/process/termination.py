"""Process tree termination (Windows / POSIX).

ProcessRunner 在 timeout 或 external cancellation 时，
必须终止整棵进程树，而不是只杀 shell 主进程。

Windows：
    通过 CREATE_NEW_PROCESS_GROUP 建立新进程组，
    再用 taskkill /T /F 沿进程树强杀所有后代。

POSIX：
    通过 start_new_session=True 让子进程成为新会话/进程组 leader，
    这样 pgid == pid，用 os.killpg 终止整组进程。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from typing import Any


def spawn_kwargs() -> dict[str, Any]:
    """返回使整树终止成为可能的 subprocess 创建参数。"""
    if sys.platform == "win32":
        return {
            "creationflags": getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        }

    return {"start_new_session": True}


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
) -> None:
    """终止给定进程及其所有后代进程。

    本函数只负责发送终止信号，不负责 reap。
    调用方仍需 await process.wait() 回收进程资源。
    """
    if sys.platform == "win32":
        await _terminate_windows(process)
    else:
        _terminate_posix(process)


async def _terminate_windows(
    process: asyncio.subprocess.Process,
) -> None:
    pid = process.pid

    if pid is None:
        return

    # taskkill /T /F 会沿进程树强杀给定 pid 的所有后代。
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID", str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except Exception:
        # taskkill 不可用或失败：退化为只杀直接子进程。
        pass

    # 兜底：确保直接子进程本身已终止。
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _terminate_posix(
    process: asyncio.subprocess.Process,
) -> None:
    pid = process.pid

    if pid is None:
        return

    # start_new_session=True 让 pgid == pid，
    # killpg 会终止整个进程组。
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
