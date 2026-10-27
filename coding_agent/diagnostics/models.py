"""Unified diagnostic model.

内部约定：
    path     = workspace-relative POSIX path
    line     = 1-based
    column   = 1-based
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


class Diagnostic(BaseModel):
    path: str | None = None

    line: int | None = None
    column: int | None = None

    end_line: int | None = None
    end_column: int | None = None

    severity: DiagnosticSeverity
    source: str
    code: str | None = None
    message: str


class DiagnosticBundle(BaseModel):
    """诊断聚合，支持 sort / group / exact dedup / limit。"""

    diagnostics: list[Diagnostic] = Field(
        default_factory=list
    )

    # --------------------------------------------------
    # 有界限制
    # --------------------------------------------------

    def limit(
        self,
        *,
        max_diagnostics: int = 200,
        max_message_chars: int = 2048,
    ) -> "DiagnosticBundle":
        """截断诊断数量与 message 长度，并标记 truncated。"""
        limited = [
            self._truncate_message(d, max_message_chars)
            for d in self.diagnostics
        ]

        result = DiagnosticBundle(
            diagnostics=limited[:max_diagnostics]
        )

        return result

    @staticmethod
    def _truncate_message(
        diagnostic: Diagnostic,
        max_chars: int,
    ) -> Diagnostic:
        if len(diagnostic.message) <= max_chars:
            return diagnostic

        return diagnostic.model_copy(
            update={
                "message": (
                    diagnostic.message[:max_chars]
                    + "... [message truncated]"
                )
            }
        )

    # --------------------------------------------------
    # sort / dedup
    # --------------------------------------------------

    def sorted(self) -> "DiagnosticBundle":
        """按 path/line/column/severity/source 稳定排序。"""
        return DiagnosticBundle(
            diagnostics=sorted(
                self.diagnostics,
                key=lambda d: (
                    d.path or "",
                    d.line if d.line is not None else 1 << 30,
                    d.column if d.column is not None else 1 << 30,
                    d.severity.value,
                    d.source,
                ),
            )
        )

    def deduplicate(self) -> "DiagnosticBundle":
        """去掉完全相同的诊断（path,line,column,source,code,message）。"""
        seen: set[tuple] = set()
        unique: list[Diagnostic] = []

        for d in self.diagnostics:
            key = (
                d.path,
                d.line,
                d.column,
                d.source,
                d.code,
                d.message,
            )

            if key in seen:
                continue

            seen.add(key)
            unique.append(d)

        return DiagnosticBundle(diagnostics=unique)

    def group_by_file(self) -> dict[str, list[Diagnostic]]:
        """按 path 分组（path 为 None 的归入 "<no-path>"）。"""
        grouped: dict[str, list[Diagnostic]] = {}

        for d in self.diagnostics:
            key = d.path or "<no-path>"
            grouped.setdefault(key, []).append(d)

        return grouped

    @property
    def count(self) -> int:
        return len(self.diagnostics)
