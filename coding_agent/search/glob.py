"""Glob runtime: workspace-bounded file/directory listing."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from coding_agent.errors import (
    InvalidArgumentError,
    PathOutsideWorkspaceError,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.models import GlobMatch, GlobResult

DEFAULT_IGNORED_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".mini_pi",
    "dist",
    "build",
})

# glob 元字符：用于切出 pattern 的静态前缀目录。
_MAGIC_CHARS = "*?["

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class GlobRuntime:
    """在 Workspace 内按 glob pattern 列出文件与目录。"""

    def __init__(
        self,
        workspace: Workspace,
        *,
        ignored_dirs: frozenset[str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.ignored_dirs = (
            ignored_dirs
            if ignored_dirs is not None
            else DEFAULT_IGNORED_DIRS
        )

    async def glob(
        self,
        pattern: str,
        *,
        max_results: int = 200,
    ) -> GlobResult:
        self._validate(pattern, max_results)

        return await asyncio.to_thread(
            self.glob_sync,
            pattern,
            max_results,
        )

    # --------------------------------------------------
    # 同步实现（供 async 入口与其它 sync 运行时复用）
    # --------------------------------------------------

    def glob_sync(
        self,
        pattern: str,
        max_results: int,
    ) -> GlobResult:
        normalized = pattern.replace("\\", "/")

        # 1. 拒绝逃逸 workspace 的 pattern。
        self._reject_escape(normalized)

        # 2. 编译成 anchor regex，正确处理 ** 语义。
        matcher = self._compile(normalized)

        matches: list[GlobMatch] = []

        for dirpath, dirnames, filenames in os.walk(
            self.workspace.root
        ):
            # 剪枝：跳过默认忽略的大目录。
            dirnames[:] = [
                name
                for name in dirnames
                if name not in self.ignored_dirs
            ]

            for name in filenames:
                abs_path = Path(dirpath) / name
                rel = self.workspace.relative(abs_path)

                if not matcher.match(rel):
                    continue

                matches.append(
                    GlobMatch(
                        path=rel,
                        is_file=True,
                        size_bytes=abs_path.stat().st_size,
                    )
                )

            for name in dirnames:
                abs_path = Path(dirpath) / name
                rel = self.workspace.relative(abs_path)

                if not matcher.match(rel):
                    continue

                matches.append(
                    GlobMatch(
                        path=rel,
                        is_file=False,
                        size_bytes=None,
                    )
                )

        # 3. 稳定排序。
        matches.sort(key=lambda item: item.path)

        truncated = len(matches) > max_results

        return GlobResult(
            pattern=pattern,
            matches=matches[:max_results],
            truncated=truncated,
        )

    def _validate(
        self,
        pattern: str,
        max_results: int,
    ) -> None:
        if not pattern or not pattern.strip():
            raise InvalidArgumentError(
                "pattern 不能为空"
            )

        if max_results <= 0:
            raise InvalidArgumentError(
                "max_results 必须大于 0"
            )

    # --------------------------------------------------
    # glob → regex
    # --------------------------------------------------

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        """把 POSIX glob pattern 编译成全匹配 regex。

        语义与 glob.glob(recursive=True) 对齐：
            **/  匹配零个或多个目录
            **   匹配任意字符（含 /）
            *    匹配除 / 外任意字符
            ?    匹配除 / 外单个字符
            [...] 字符类
        """
        i = 0
        n = len(pattern)
        out: list[str] = []

        while i < n:
            char = pattern[i]

            if char == "*":
                if i + 1 < n and pattern[i + 1] == "*":
                    # **
                    j = i + 2
                    if j < n and pattern[j] == "/":
                        out.append("(?:[^/]*/)*")
                        i = j + 1
                    else:
                        out.append(".*")
                        i = j
                else:
                    out.append("[^/]*")
                    i += 1

            elif char == "?":
                out.append("[^/]")
                i += 1

            elif char == "[":
                j = i + 1

                if j < n and pattern[j] in "!^":
                    j += 1
                if j < n and pattern[j] == "]":
                    j += 1
                while j < n and pattern[j] != "]":
                    j += 1

                if j >= n:
                    out.append(r"\[")
                    i += 1
                else:
                    stuff = pattern[i + 1:j].replace("\\", "\\\\")
                    i = j
                    if stuff[0] == "!":
                        stuff = "^" + stuff[1:]
                    elif stuff[0] == "^":
                        stuff = "\\" + stuff
                    out.append(f"[{stuff}]")
                    i += 1

            else:
                out.append(re.escape(char))
                i += 1

        return re.compile(
            "^" + "".join(out) + "$"
        )

    # --------------------------------------------------
    # workspace escape 拒绝
    # --------------------------------------------------

    def _reject_escape(self, normalized: str) -> None:
        # POSIX 绝对路径。
        if normalized.startswith("/"):
            raise InvalidArgumentError(
                f"pattern 必须是 workspace 相对路径: {normalized}"
            )

        # Windows drive / UNC。
        if (
            _WINDOWS_DRIVE.match(normalized)
            or normalized.startswith("//")
        ):
            raise InvalidArgumentError(
                f"pattern 必须是 workspace 相对路径: {normalized}"
            )

        # 通过静态前缀目录做 containment 检查，捕捉 ../../ 逃逸。
        prefix = self._static_prefix(normalized)
        prefix_dir = self._dirname(prefix) or "."

        try:
            self.workspace.resolve(prefix_dir)
        except PathOutsideWorkspaceError as exc:
            raise InvalidArgumentError(
                f"pattern 逃逸 workspace: {normalized}"
            ) from exc
        except ValueError as exc:
            raise InvalidArgumentError(
                f"pattern 必须是 workspace 相对路径: {normalized}"
            ) from exc

    @staticmethod
    def _static_prefix(normalized: str) -> str:
        """返回第一个 glob 元字符之前的字面部分。"""
        index = len(normalized)

        for magic in _MAGIC_CHARS:
            pos = normalized.find(magic)
            if pos != -1 and pos < index:
                index = pos

        return normalized[:index]

    @staticmethod
    def _dirname(prefix: str) -> str:
        """返回静态前缀的目录部分（POSIX 风格）。"""
        prefix = prefix.rstrip("/")

        slash = prefix.rfind("/")

        if slash == -1:
            return ""

        return prefix[:slash]
