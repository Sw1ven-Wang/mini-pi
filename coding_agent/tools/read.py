"""Read-file tool."""

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.filesystem.workspace import (
    Workspace,
)

from coding_agent.filesystem.snapshots import (
    snapshot_file,
)


class ReadTool(AgentTool):
    """读取指定路径的 UTF-8 文本文件。"""

    name = "read"

    capabilities =frozenset({
        "read_fs",
    })
    description = (
        "读取指定路径的文本文件内容（UTF-8）。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要读取的文件路径",
            },
        },
        "required": [
            "path",
        ],
    }



    def __init__(
        self,
        workspace: Workspace,
    ) -> None:
        self.workspace = workspace


    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:

        path = arguments.get(
            "path"
        )

        if not path:
            raise ValueError(
                "缺少参数: path"
            )


        file_path = self.workspace.resolve(
            path
        )


        if not file_path.exists():
            raise FileNotFoundError(
                f"文件不存在: {file_path}"
            )


        snapshot = snapshot_file(
            file_path,
            workspace_relative=path,
        )


        return AgentToolResult(
            content=snapshot.content,
            details={
                "snapshot": snapshot.model_dump(),
            },
        )