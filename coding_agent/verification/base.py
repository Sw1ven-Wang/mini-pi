"""Verifier and DiagnosticParser protocols.

Verifier 是 Runtime service，不是 AgentTool。
"""

from __future__ import annotations

from typing import Protocol

from coding_agent.diagnostics.models import Diagnostic
from coding_agent.filesystem.workspace import Workspace
from coding_agent.verification.models import (
    VerificationKind,
    VerificationResult,
)


class Verifier(Protocol):
    name: str
    kind: VerificationKind

    async def verify(
        self,
        *,
        workspace: Workspace,
    ) -> VerificationResult:
        ...


class DiagnosticParser(Protocol):
    """把 verifier 的 stdout/stderr 解析成结构化诊断。

    必须是 best-effort：解析失败不得 crash，
    允许返回空 diagnostics。
    """

    def parse(
        self,
        *,
        stdout: str,
        stderr: str,
        workspace: Workspace,
    ) -> list[Diagnostic]:
        ...
