from __future__ import annotations

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.errors import (
    InvalidArgumentError,
    ProcessTimeoutError,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import (
    powershell_config,
)


class PowerShellTool(AgentTool):
    name = "powershell"

    description = (
        "在 Windows PowerShell 中执行命令。"
        "用于运行测试、lint、构建命令和项目检查。"
        "必须使用 PowerShell 语法。"
        "文件名应原样输出，不要添加 Markdown 转义。"
    )

    capabilities = frozenset({
        "execute",
    })

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 PowerShell 命令",
            },
            "cwd": {
                "type": "string",
                "description": "workspace 内工作目录",
                "default": ".",
            },
            "timeout_seconds": {
                "type": "number",
                "default": 30,
            },
            "max_output_bytes": {
                "type": "integer",
                "default": 100_000,
            },
        },
        "required": [
            "command",
        ],
    }

    def __init__(
        self,
        workspace: Workspace,
        process_runner: ProcessRunner,
    ) -> None:
        self.workspace = workspace
        self.process_runner = process_runner
        self.shell_config = powershell_config()

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:

        command = arguments.get("command")

        if (
            not isinstance(command, str)
            or not command.strip()
        ):
            raise InvalidArgumentError(
                "command 不能为空"
            )

        cwd_argument = arguments.get(
            "cwd",
            ".",
        )

        timeout_seconds = arguments.get(
            "timeout_seconds",
            30,
        )

        max_output_bytes = arguments.get(
            "max_output_bytes",
            100_000,
        )

        cwd = self.workspace.resolve(
            cwd_argument
        )

        if not cwd.is_dir():
            raise InvalidArgumentError(
                f"cwd 不是目录: {cwd_argument}"
            )

        result = await self.process_runner.run(
            command,
            cwd=cwd,
            shell_config=self.shell_config,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )

        if result.timed_out:
            raise ProcessTimeoutError(
                command=command,
                timeout_seconds=timeout_seconds,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        parts = [
            f"exit_code: {result.exit_code}",
        ]

        if result.stdout:
            parts.append(
                "stdout:\n"
                + result.stdout
            )

        if result.stderr:
            parts.append(
                "stderr:\n"
                + result.stderr
            )

        if result.truncated:
            parts.append(
                "[output truncated]"
            )

        return AgentToolResult(
            content="\n\n".join(parts),
            details={
                "command": result.command,
                "cwd": str(cwd),
                "shell": "powershell",
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
                "truncated": result.truncated,
            },
        )