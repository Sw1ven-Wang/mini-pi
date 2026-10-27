"""Process runner with bounded output, timeout, and tree termination."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import json
from pathlib import Path
from collections.abc import Mapping
from uuid import uuid4

from pydantic import BaseModel

from coding_agent.errors import ProcessStartFailedError
from coding_agent.process.shell import (
    ShellConfig,
    default_shell_config,
)
from coding_agent.process.termination import (
    spawn_kwargs,
    terminate_process_tree,
)
from coding_agent.testing.fault_injection import NoopFaultInjector


class ProcessResult(BaseModel):
    command: str

    exit_code: int | None

    stdout: str
    stderr: str

    timed_out: bool = False
    truncated: bool = False


class OutputAccumulator:
    """有界输出累加器。

    增量读入 stdout/stderr 分片，内存占用被限制在约
    max_bytes 量级，而不是进程真实输出总量。

    语义（head + tail）：

    - 总字节数 <= max_bytes：保留完整输出，truncated = False。
    - 总字节数 >  max_bytes：只保留前 head_bytes 与后 tail_bytes，
      中间省略，truncated = True。
    """

    _MARKER = b"\n...[output truncated]...\n"

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.head_bytes = max_bytes // 2
        self.tail_bytes = max_bytes - self.head_bytes

        self._buffer = bytearray()
        self._head = bytearray()
        self._tail = bytearray()

        self._total = 0
        self._truncated = False

    def feed(self, chunk: bytes) -> None:
        """读入一个输出分片。"""
        if not chunk:
            return

        self._total += len(chunk)

        if not self._truncated:
            # 尚未超过上限：继续完整累积。
            self._buffer.extend(chunk)

            if len(self._buffer) > self.max_bytes:
                # 第一次越界：切到 head + tail 模式。
                self._truncated = True
                self._head = self._buffer[: self.head_bytes]
                self._tail = self._buffer[-self.tail_bytes :]
                self._buffer = bytearray()

            return

        # 已截断：只维护尾部窗口。
        self._tail.extend(chunk)
        overflow = len(self._tail) - self.tail_bytes
        if overflow > 0:
            del self._tail[:overflow]

    @property
    def truncated(self) -> bool:
        return self._truncated

    def result(self) -> tuple[str, bool]:
        """返回解码后的文本以及是否被截断。"""
        if not self._truncated:
            return (
                bytes(self._buffer).decode(
                    "utf-8",
                    errors="replace",
                ),
                False,
            )

        payload = (
            bytes(self._head)
            + self._MARKER
            + bytes(self._tail)
        )

        return (
            payload.decode("utf-8", errors="replace"),
            True,
        )


async def _read_stream(
    stream: asyncio.StreamReader,
    accumulator: OutputAccumulator,
) -> None:
    """增量读一个输出流，直到 EOF。"""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return
        accumulator.feed(chunk)


class ProcessRunner:
    """通过显式 ShellConfig 运行命令，并保证输出/生命周期安全。"""

    def __init__(self, fault_injector=None, *, parent_lifetime_guard=False, resource_recorder=None) -> None:
        self.fault_injector = fault_injector or NoopFaultInjector()
        self.parent_lifetime_guard = parent_lifetime_guard
        self.resource_recorder = resource_recorder

    async def run(self, command: str, **kwargs) -> ProcessResult:
        # Reserve before spawning: a crash in the spawn/ownership gap is unknown,
        # never evidence that no process exists. No command/env enters the lease.
        lease = {"lease_id": str(uuid4()), "state": "reserved", "platform": os.name,
                 "job_name": None, "ownership_protocol": 1}
        if self.resource_recorder:
            self.resource_recorder(lease)
        try:
            return await self._run(command, lease=lease, **kwargs)
        finally:
            if self.resource_recorder and lease["state"] == "owned":
                stopped = False
                if os.name == "nt" and lease.get("job_name"):
                    from coding_agent.process.windows_job import job_is_stopped
                    stopped = job_is_stopped(lease["job_name"])
                elif os.name != "nt" and lease.get("process_group"):
                    try:
                        os.killpg(lease["process_group"], 0)
                    except ProcessLookupError:
                        stopped = True
                    except PermissionError:
                        pass
                if stopped:
                    self.resource_recorder({**lease, "state": "stopped"})

    async def _run(
        self,
        command: str,
        *,
        cwd: Path,
        shell_config: ShellConfig | None = None,
        timeout_seconds: float = 30,
        max_output_bytes: int = 100_000,
        env: Mapping[str, str] | None = None,
        lease: dict | None = None,
    ) -> ProcessResult:
        self.fault_injector.maybe_fail("process_timeout")
        config = (
            shell_config
            or default_shell_config()
        )

        if not command:
            raise ValueError(
                "command 不能为空"
            )

        if timeout_seconds <= 0:
            raise ValueError(
                "timeout_seconds 必须大于 0"
            )

        if max_output_bytes <= 0:
            raise ValueError(
                "max_output_bytes 必须大于 0"
            )

        if not cwd.exists():
            raise FileNotFoundError(
                f"cwd 不存在: {cwd}"
            )

        if not cwd.is_dir():
            raise ValueError(
                f"cwd 不是目录: {cwd}"
            )

        # ------------------------------------------
        # 启动进程：shell executable 不存在时，
        # 必须返回领域错误而不是泄漏 traceback。
        # ------------------------------------------

        gated = self.parent_lifetime_guard and os.name == "nt"
        # A Windows venv python.exe can itself be a redirector that forks before
        # attachment. Spawn the real base interpreter, isolated from startup hooks.
        spawn_args = ([sys._base_executable, "-I", "-S", str(Path(__file__).with_name("owned_worker.py"))]
                      if gated else [config.executable, *config.args, command])
        payload = json.dumps({"executable": config.executable, "args": config.args, "command": command}).encode()
        if gated and len(payload) > 1024 * 1024:
            raise ValueError("owned verification command exceeds stdin bridge limit")
        try:
            process = await asyncio.create_subprocess_exec(
                *spawn_args,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE if gated else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=(dict(env) if env is not None else None),
                **spawn_kwargs(),
            )
        except FileNotFoundError as exc:
            raise ProcessStartFailedError(
                command=command,
                executable=config.executable,
                reason=str(exc),
            ) from exc
        except OSError as exc:
            raise ProcessStartFailedError(
                command=command,
                executable=config.executable,
                reason=str(exc),
            ) from exc

        job = None
        if self.parent_lifetime_guard and os.name == "nt":
            from coding_agent.process.windows_job import WindowsProcessJob
            try:
                job_name = "Local\\mini-pi-" + lease["lease_id"] if self.resource_recorder else None
                job = WindowsProcessJob(process.pid, name=job_name)
                if self.resource_recorder:
                    lease.update(state="owned", job_name=job_name)
                    self.resource_recorder(lease)
                # Only now can the fixed worker launch the actual shell. A host
                # crash before this point leaves no unowned command descendants.
                process.stdin.write(payload)
                await asyncio.wait_for(process.stdin.drain(), timeout=timeout_seconds)
                process.stdin.close()
            except BaseException as exc:
                await terminate_process_tree(process)
                await process.wait()
                if job is not None:
                    job.close()
                if isinstance(exc, OSError):
                    raise ProcessStartFailedError(command=command, executable=config.executable,
                        reason="cannot establish process lifetime ownership") from exc
                raise

        if self.resource_recorder and os.name != "nt":
            try:
                lease.update(state="owned", process_group=process.pid)
                self.resource_recorder(lease)
            except BaseException:
                await terminate_process_tree(process)
                await process.wait()
                raise

        stdout_acc = OutputAccumulator(max_output_bytes)
        stderr_acc = OutputAccumulator(max_output_bytes)

        timed_out = False

        async def drain() -> None:
            await asyncio.gather(
                _read_stream(process.stdout, stdout_acc),
                _read_stream(process.stderr, stderr_acc),
            )

        try:
            await asyncio.wait_for(
                drain(),
                timeout=timeout_seconds,
            )

            # stdout/stderr 已 EOF，说明主进程及其继承
            # 管道的后代都已关闭，这里负责 reap。
            await process.wait()

        except asyncio.TimeoutError:
            # timeout：终止整棵进程树，不伪装成取消。
            timed_out = True
            await terminate_process_tree(process)

            with contextlib.suppress(Exception):
                await process.wait()

        except asyncio.CancelledError:
            # external cancellation：终止整棵树后重新抛出，
            # 绝不吞掉 CancelledError，也不伪装成 timeout。
            await terminate_process_tree(process)

            with contextlib.suppress(Exception):
                await process.wait()

            raise

        finally:
            if job is not None:
                job.close()

        stdout_text, stdout_truncated = stdout_acc.result()
        stderr_text, stderr_truncated = stderr_acc.result()

        return ProcessResult(
            command=command,
            exit_code=process.returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            timed_out=timed_out,
            truncated=(
                stdout_truncated
                or stderr_truncated
            ),
        )
