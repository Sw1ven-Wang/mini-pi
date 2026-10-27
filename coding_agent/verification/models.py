"""Verification result models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from coding_agent.changes.models import ChangedRange, MutationResult
from coding_agent.diagnostics.models import Diagnostic


class VerificationKind(str, Enum):
    TEST = "test"
    LINT = "lint"
    FORMAT = "format"
    TYPECHECK = "typecheck"
    CUSTOM = "custom"


class VerificationResult(BaseModel):
    kind: VerificationKind
    name: str
    passed: bool

    command: str | None = None
    exit_code: int | None = None

    diagnostics: list[Diagnostic] = Field(
        default_factory=list
    )

    stdout: str = ""
    stderr: str = ""

    duration_ms: int | None = None
    truncated: bool = False


class VerificationSummary(BaseModel):
    passed: bool
    results: list[VerificationResult]
    diagnostics: list[Diagnostic] = Field(
        default_factory=list
    )


class VerificationScope(BaseModel):
    """定向验证范围（第一版 verifier 可忽略，接口保留）。"""

    changed_files: list[str] = Field(
        default_factory=list
    )
    changed_ranges: list[ChangedRange] = Field(
        default_factory=list
    )


class FormatterResult(BaseModel):
    """Formatter 运行结果：报告是否修改了文件以及 mutation 详情。"""

    changed: bool
    mutations: list[MutationResult] = Field(
        default_factory=list
    )
    verification: VerificationResult | None = None
