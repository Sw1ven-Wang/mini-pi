"""Literal text search runtime (first version, no regex / semantic search)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from coding_agent.errors import InvalidArgumentError
from coding_agent.filesystem.encoding import decode_text_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.glob import GlobRuntime
from coding_agent.search.models import SearchMatch, SearchResult

# 单文件大小上限（bytes），超过跳过，避免把超大文件读入内存。
DEFAULT_MAX_FILE_BYTES = 1_000_000

# preview 单行长度上限。
DEFAULT_PREVIEW_MAX_CHARS = 200

# 明显 binary 的探测：前 8192 字节里出现 NUL。
_BINARY_PROBE_BYTES = 8192


class TextSearchRuntime:
    """在 Workspace 内做 literal string search。"""

    def __init__(
        self,
        workspace: Workspace,
        *,
        glob_runtime: GlobRuntime | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        preview_max_chars: int = DEFAULT_PREVIEW_MAX_CHARS,
    ) -> None:
        self.workspace = workspace
        self.glob_runtime = glob_runtime or GlobRuntime(workspace)
        self.max_file_bytes = max_file_bytes
        self.preview_max_chars = preview_max_chars

    async def search_text(
        self,
        query: str,
        *,
        glob: str | None = None,
        max_results: int = 100,
        context_lines: int = 2,
        allowed_paths: frozenset[str] | None = None,
    ) -> SearchResult:
        if not query:
            raise InvalidArgumentError(
                "query 不能为空"
            )

        if max_results <= 0:
            raise InvalidArgumentError(
                "max_results 必须大于 0"
            )

        return await asyncio.to_thread(
            self._search_sync,
            query,
            glob,
            max_results,
            context_lines,
            allowed_paths,
        )

    # --------------------------------------------------
    # 同步实现
    # --------------------------------------------------

    def _search_sync(
        self,
        query: str,
        glob: str | None,
        max_results: int,
        context_lines: int,
        allowed_paths: frozenset[str] | None = None,
    ) -> SearchResult:
        # 1. 先通过 glob 收集候选文件（目录会被 is_file=False 过滤）。
        glob_pattern = glob or "**/*"

        listing = self.glob_runtime.glob_sync(
            glob_pattern,
            max_results=10_000,
        )

        matches: list[SearchMatch] = []
        truncated = False

        for item in listing.matches:
            if not item.is_file:
                continue
            if allowed_paths is not None and item.path not in allowed_paths:
                continue

            file_path = self.workspace.resolve(item.path)

            # 单文件大小限制。
            try:
                size = file_path.stat().st_size
            except OSError:
                continue

            if size > self.max_file_bytes:
                continue

            try:
                data = file_path.read_bytes()
            except OSError:
                continue

            # 明显 binary 跳过。
            if b"\x00" in data[: _BINARY_PROBE_BYTES]:
                continue

            # UTF-8 / BOM 解码；无法解码视为 binary 跳过。
            try:
                decoded = decode_text_file(data)
            except ValueError:
                continue

            text = decoded.content

            if query not in text:
                continue

            lines = text.split("\n")

            for index, line in enumerate(lines):
                column = line.find(query)

                if column == -1:
                    continue

                matches.append(
                    SearchMatch(
                        path=item.path,
                        line=index + 1,
                        column=column + 1,
                        preview=self._preview(
                            lines,
                            index,
                            query,
                            context_lines,
                        ),
                    )
                )

                if len(matches) >= max_results:
                    truncated = True
                    break

            if truncated:
                break

        # 稳定排序：按 path + line。
        matches.sort(
            key=lambda m: (m.path, m.line)
        )

        return SearchResult(
            query=query,
            matches=matches,
            truncated=truncated,
        )

    def _preview(
        self,
        lines: list[str],
        index: int,
        query: str,
        context_lines: int,
    ) -> str:
        """生成带上下文的 preview，长度受限。"""
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)

        preview = "\n".join(lines[start:end])

        if len(preview) > self.preview_max_chars:
            preview = preview[: self.preview_max_chars]

        return preview
