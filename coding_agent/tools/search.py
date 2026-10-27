"""Search tool: literal text search within the workspace."""

from __future__ import annotations

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.text_search import TextSearchRuntime


class SearchTool(AgentTool):
    name = "search"

    description = (
        "在 workspace 内做字面文本搜索（literal string）。"
        "返回匹配文件、行号与上下文预览。"
    )

    capabilities = frozenset({
        "read_fs",
        "search_fs",
    })

    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要搜索的字面字符串",
            },
            "glob": {
                "type": "string",
                "description": (
                    "可选，限定搜索范围，例如 **/*.py"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "最大匹配条数",
                "default": 100,
            },
            "context_lines": {
                "type": "integer",
                "description": "预览上下文的行数",
                "default": 2,
            },
        },
        "required": [
            "query",
        ],
    }

    def __init__(
        self,
        workspace: Workspace,
    ) -> None:
        self.workspace = workspace
        self.runtime = TextSearchRuntime(workspace)

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        query = arguments.get("query")

        glob_pattern = arguments.get("glob")

        max_results = arguments.get(
            "max_results",
            100,
        )

        context_lines = arguments.get(
            "context_lines",
            2,
        )

        result = await self.runtime.search_text(
            query,
            glob=glob_pattern,
            max_results=max_results,
            context_lines=context_lines,
        )

        # --------------------------------------------------
        # 简洁文本：path:line + preview，不含绝对路径。
        # --------------------------------------------------

        lines = [
            f"Found {len(result.matches)} matches:",
            "",
        ]

        for match in result.matches:
            lines.append(f"{match.path}:{match.line}")
            lines.append(f"  {match.preview.strip()}")

        if result.truncated:
            lines.append(
                f"... [truncated at {max_results}]"
            )

        return AgentToolResult(
            content="\n".join(lines),
            details={
                "query": result.query,
                "matches": [
                    match.model_dump()
                    for match in result.matches
                ],
                "truncated": result.truncated,
            },
        )
