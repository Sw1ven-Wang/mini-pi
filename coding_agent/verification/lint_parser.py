"""Ruff lint output parser.

优先解析结构化 JSON（ruff check --output-format json），
不做 regex 解析人类文本。
"""

from __future__ import annotations

import json

from coding_agent.diagnostics.models import (
    Diagnostic,
    DiagnosticSeverity,
)
from coding_agent.filesystem.workspace import Workspace


class RuffParser:
    source = "ruff"

    def parse(
        self,
        *,
        stdout: str,
        stderr: str,
        workspace: Workspace,
    ) -> list[Diagnostic]:
        try:
            entries = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return []

        if not isinstance(entries, list):
            return []

        diagnostics: list[Diagnostic] = []

        for entry in entries:
            diagnostic = self._convert(entry, workspace)

            if diagnostic is not None:
                diagnostics.append(diagnostic)

        return diagnostics

    def _convert(
        self,
        entry: dict,
        workspace: Workspace,
    ) -> Diagnostic | None:
        filename = entry.get("filename")

        if not isinstance(filename, str):
            return None

        normalized = self._normalize_path(filename, workspace)

        if normalized is None:
            return None

        location = entry.get("location") or {}

        row = location.get("row")
        column = location.get("column")

        line = int(row) if isinstance(row, int) else None
        column = int(column) if isinstance(column, int) else None

        # ruff 已 1-based。
        code = entry.get("code")
        message = entry.get("message", "")

        return Diagnostic(
            path=normalized,
            line=line,
            column=column,
            severity=DiagnosticSeverity.WARNING,
            source=self.source,
            code=str(code) if code is not None else None,
            message=str(message),
        )

    @staticmethod
    def _normalize_path(
        raw: str,
        workspace: Workspace,
    ) -> str | None:
        normalized = raw.replace("\\", "/")

        try:
            resolved = workspace.resolve(normalized)
            return workspace.relative(resolved)
        except Exception:
            return None
