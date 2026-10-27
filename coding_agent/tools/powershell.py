from __future__ import annotations

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.errors import (
    CommandPolicyBlockedError,
    InvalidArgumentError,
    ProcessTimeoutError,
)
from coding_agent.safety.command_policy import (
    CommandDecisionType,
    CommandPolicy,
)
from coding_agent.safety.shell_ir import ShellKind
from coding_agent.sandbox.base import Sandbox
from coding_agent.sandbox.local import LocalSandbox
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
                "maxLength": 100000,
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
        command_policy: CommandPolicy | None = None,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.workspace = workspace
        self.process_runner = process_runner
        self.shell_config = powershell_config()
        self.command_policy = command_policy or CommandPolicy(shell=ShellKind.POWERSHELL)
        self.sandbox = sandbox or LocalSandbox(
            process_runner=process_runner,
            shell_config=self.shell_config,
        )

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:

        command = arguments.get("command")

        if (
            not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
            or len(command.encode("utf-8")) > 100_000
        ):
            raise InvalidArgumentError(
                "command 不能为空、包含 NUL 或超过 100000 bytes"
            )

        policy_decision = self.command_policy.evaluate(command)
        if policy_decision.decision == CommandDecisionType.DENY:
            raise CommandPolicyBlockedError(
                risk=policy_decision.risk.value,
                rule_id=policy_decision.rule_id,
                reason=policy_decision.reason,
                suggestion=policy_decision.suggestion,
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

        if (
            not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > 300
        ):
            raise InvalidArgumentError(
                "timeout_seconds 必须位于 (0, 300] 秒"
            )

        if (
            not isinstance(max_output_bytes, int)
            or max_output_bytes <= 0
            or max_output_bytes > 200_000
        ):
            raise InvalidArgumentError(
                "max_output_bytes 必须位于 "
                "(0, 200000] 范围"
            )

        cwd = self.workspace.resolve(
            cwd_argument
        )

        if not cwd.exists():
            raise InvalidArgumentError(
                f"cwd 不存在: {cwd_argument}"
            )

        if not cwd.is_dir():
            raise InvalidArgumentError(
                f"cwd 不是目录: {cwd_argument}"
            )

        sandbox_session = await self.sandbox.start(
            workspace=Workspace(cwd)
        )
        try:
            result = await sandbox_session.run(
                command,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
            )
        finally:
            await sandbox_session.stop()

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
                "command_policy": {
                    "risk": policy_decision.risk.value,
                    "decision": policy_decision.decision.value,
                    "rule_id": policy_decision.rule_id,
                },
                "sandbox": sandbox_session.resource_policy.model_dump(mode="json"),
            },
        )
