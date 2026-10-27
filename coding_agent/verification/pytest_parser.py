"""Pytest output parser (best-effort).

第一版只解析常见格式：
    path.py:line: ErrorType
    FAILED path::test_name
    AssertionError

不依赖 subprocess，输入是 stdout/stderr 字符串。
"""

from __future__ import annotations

import re

from coding_agent.diagnostics.models import (
    Diagnostic,
    DiagnosticSeverity,
)
from coding_agent.filesystem.workspace import Workspace

# path.py:line: ErrorType
# 例如：tests/test_calc.py:8: AssertionError
_LOCATION_RE = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+):\s*(?P<message>.+)$"
)

# FAILED path::test_name - message
# 例如：FAILED tests/test_calc.py::test_add - assert -1 == 5
_FAILED_RE = re.compile(
    r"^FAILED\s+(?P<target>[^\s]+)(?:\s+-\s+(?P<message>.+))?$"
)


class PytestParser:
    source = "pytest"

    def parse(
        self,
        *,
        stdout: str,
        stderr: str,
        workspace: Workspace,
    ) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        seen: set[tuple] = set()

        for line in (stdout + "\n" + stderr).splitlines():
            diagnostic = self._parse_line(line, workspace)

            if diagnostic is None:
                continue

            key = (
                diagnostic.path,
                diagnostic.line,
                diagnostic.column,
                diagnostic.message,
            )

            if key in seen:
                continue

            seen.add(key)
            diagnostics.append(diagnostic)

        return diagnostics

    def _parse_line(
        self,
        line: str,
        workspace: Workspace,
    ) -> Diagnostic | None:
        stripped = line.strip()

        if not stripped:
            return None

        # --------------------------------------------------
        # 1. path.py:line: ErrorType
        # --------------------------------------------------

        match = _LOCATION_RE.match(stripped)

        if match:
            path = match.group("path")

            # Windows path 转 workspace-relative POSIX。
            normalized = self._normalize_path(path, workspace)

            if normalized is None:
                return None

            try:
                line_number = int(match.group("line"))
            except ValueError:
                return None

            return Diagnostic(
                path=normalized,
                line=line_number,
                column=None,
                severity=DiagnosticSeverity.ERROR,
                source=self.source,
                code=None,
                message=match.group("message").strip(),
            )

        # --------------------------------------------------
        # 2. FAILED path::test_name - message
        # --------------------------------------------------

        match = _FAILED_RE.match(stripped)

        if match:
            target = match.group("target")

            # target 形如 tests/test_calc.py::test_add
            path = target.split("::", 1)[0]

            normalized = self._normalize_path(path, workspace)

            if normalized is None:
                return None

            message = (
                match.group("message")
                or target
            )

            return Diagnostic(
                path=normalized,
                line=None,
                column=None,
                severity=DiagnosticSeverity.ERROR,
                source=self.source,
                code=None,
                message=message.strip(),
            )

        # --------------------------------------------------
        # 3. 兜底：AssertionError 无位置信息
        # --------------------------------------------------

        if "AssertionError" in stripped:
            return Diagnostic(
                path=None,
                line=None,
                column=None,
                severity=DiagnosticSeverity.ERROR,
                source=self.source,
                code=None,
                message=stripped,
            )

        return None

    @staticmethod
    def _normalize_path(
        raw: str,
        workspace: Workspace,
    ) -> str | None:
        """把任意形式的 path 规范成 workspace-relative POSIX path。

        返回 None 表示无法映射到 workspace 内，应丢弃。
        """
        normalized = raw.replace("\\", "/")

        # 已经是相对路径：直接校验 containment。
        try:
            resolved = workspace.resolve(normalized)
            return workspace.relative(resolved)
        except Exception:
            return None
