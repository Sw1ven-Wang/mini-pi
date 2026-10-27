"""预配置 verifier 工厂（host-defined，静态 command）。"""

from __future__ import annotations

from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import ShellConfig
from coding_agent.sandbox.base import Sandbox
from coding_agent.verification.command import CommandVerifier
from coding_agent.verification.lint_parser import RuffParser
from coding_agent.verification.models import VerificationKind
from coding_agent.verification.pytest_parser import PytestParser


def pytest_verifier(
    process_runner: ProcessRunner,
    shell_config: ShellConfig,
    *,
    command: str = "uv run pytest -q",
    timeout_seconds: float = 120,
    sandbox: Sandbox | None = None,
) -> CommandVerifier:
    return CommandVerifier(
        name="pytest",
        kind=VerificationKind.TEST,
        command=command,
        process_runner=process_runner,
        shell_config=shell_config,
        parser=PytestParser(),
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
    )


def ruff_verifier(
    process_runner: ProcessRunner,
    shell_config: ShellConfig,
    *,
    command: str = "uv run ruff check . --output-format json",
    timeout_seconds: float = 120,
    sandbox: Sandbox | None = None,
) -> CommandVerifier:
    return CommandVerifier(
        name="ruff",
        kind=VerificationKind.LINT,
        command=command,
        process_runner=process_runner,
        shell_config=shell_config,
        parser=RuffParser(),
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
    )
