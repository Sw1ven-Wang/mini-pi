"""Glob tool: list files by glob pattern within the workspace."""

from __future__ import annotations

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.glob import GlobRuntime


class GlobTool(AgentTool):
    name = "glob"

    description = (
        "按 glob pattern 查找 workspace 内的文件和目录。"
        "返回 workspace 相对路径，结果稳定排序。"
    )

    capabilities = frozenset({
        "read_fs",
        "search_fs",
    })

    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "glob pattern，例如 **/*.py 或 src/**/*.py"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回结果数",
                "default": 200,
            },
        },
        "required": [
            "pattern",
        ],
    }

    def __init__(
        self,
        workspace: Workspace,
    ) -> None:
        self.workspace = workspace
        self.runtime = GlobRuntime(workspace)

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        pattern = arguments.get("pattern")

        max_results = arguments.get(
            "max_results",
            200,
        )

        result = await self.runtime.glob(
            pattern,
            max_results=max_results,
        )

        # --------------------------------------------------
        # 给模型看的简洁文本：只保留 workspace-relative path。
        # --------------------------------------------------

        lines = [
            f"Found {len(result.matches)} items:",
            "",
        ]

        for match in result.matches:
            lines.append(match.path)

        if result.truncated:
            lines.append(
                f"... [truncated at {max_results}]"
            )

        return AgentToolResult(
            content="\n".join(lines),
            details={
                "pattern": result.pattern,
                "matches": [
                    match.model_dump()
                    for match in result.matches
                ],
                "truncated": result.truncated,
            },
        )
