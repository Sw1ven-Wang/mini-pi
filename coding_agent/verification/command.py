"""CommandVerifier: 把一次 shell 命令解释成 VerificationResult。"""

from __future__ import annotations

import time

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import ShellConfig
from coding_agent.sandbox.base import Sandbox
from coding_agent.sandbox.local import LocalSandbox
from coding_agent.verification.base import DiagnosticParser
from coding_agent.verification.models import (
    VerificationKind,
    VerificationResult,
)


class CommandVerifier:
    """预定义的 host-trusted 验证命令。"""

    def __init__(
        self,
        *,
        name: str,
        kind: VerificationKind,
        command: str,
        process_runner: ProcessRunner,
        shell_config: ShellConfig,
        parser: DiagnosticParser | None = None,
        timeout_seconds: float = 120,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.command = command
        self.process_runner = process_runner
        self.shell_config = shell_config
        self.parser = parser
        self.timeout_seconds = timeout_seconds
        self.sandbox = sandbox or LocalSandbox(
            process_runner=process_runner, shell_config=shell_config,
        )

    async def verify(
        self,
        *,
        workspace: Workspace,
    ) -> VerificationResult:
        start = time.monotonic()

        # The command is host-defined, but the tests it loads are workspace
        # code. They must use the same isolation and sanitized environment.
        sandbox_session = await self.sandbox.start(workspace=workspace)
        try:
            result = await sandbox_session.run(
                self.command, timeout_seconds=self.timeout_seconds,
            )
        finally:
            await sandbox_session.stop()

        duration_ms = int(
            (time.monotonic() - start) * 1000
        )

        diagnostics = []

        if self.parser is not None:
            # parser 是 best-effort，不得 crash。
            try:
                diagnostics = self.parser.parse(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    workspace=workspace,
                )
            except Exception:
                diagnostics = []

        return VerificationResult(
            kind=self.kind,
            name=self.name,
            passed=(result.exit_code == 0),
            command=self.command,
            exit_code=result.exit_code,
            diagnostics=diagnostics,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=duration_ms,
            truncated=result.truncated,
        )
