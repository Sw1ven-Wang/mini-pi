import asyncio
from pathlib import Path

from pydantic import BaseModel
from coding_agent.process.shell import (
    ShellConfig,
    default_shell_config,
)

def _truncate_output(
    data: bytes,
    max_bytes: int,
) -> tuple[bytes, bool]:
    """限制返回给 Agent 的输出大小。"""

    if len(data) <= max_bytes:
        return data, False

    return data[:max_bytes], True

class ProcessResult(BaseModel):
    command: str

    exit_code: int | None

    stdout: str
    stderr: str

    timed_out: bool = False
    truncated: bool = False



class ProcessRunner:

    async def run(
        self,
        command: str,
        *,
        cwd: Path,
        shell_config: ShellConfig | None = None,
        timeout_seconds: float = 30,
        max_output_bytes: int = 100_000,
    ) -> ProcessResult:
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

        process = await asyncio.create_subprocess_exec(
            config.executable,
            *config.args,
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False

        try:
            stdout_bytes, stderr_bytes = (
                await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds,
                )
            )

        except asyncio.TimeoutError:
            timed_out = True

            process.kill()

            # kill 后仍然要 await，
            # 否则可能留下未回收的子进程资源。
            stdout_bytes, stderr_bytes = (
                await process.communicate()
            )

        stdout_bytes, stdout_truncated = (
            _truncate_output(
                stdout_bytes,
                max_output_bytes,
            )
        )

        stderr_bytes, stderr_truncated = (
            _truncate_output(
                stderr_bytes,
                max_output_bytes,
            )
        )

        return ProcessResult(
            command=command,
            exit_code=process.returncode,
            stdout=stdout_bytes.decode(
                "utf-8",
                errors="replace",
            ),
            stderr=stderr_bytes.decode(
                "utf-8",
                errors="replace",
            ),
            timed_out=timed_out,
            truncated=(
                stdout_truncated
                or stderr_truncated
            ),
        )
