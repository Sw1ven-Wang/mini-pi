from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.diff import unified_diff
from coding_agent.changes.models import MutationResult
from coding_agent.filesystem.atomic_write import atomic_write_bytes
from coding_agent.filesystem.encoding import encode_text_file
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)

class WriteTool(AgentTool):

    name = "write"

    description = (
        "创建新的文本文件。"
        "如果目标文件已经存在则拒绝，"
        "修改已有文件应使用 edit 工具。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要创建的 workspace 相对路径",
            },
            "content": {
                "type": "string",
                "description": "文件内容",
            },
            "create_only": {
                "type": "boolean",
                "default": True,
            },
        },
        "required": [
            "path",
            "content",
        ],
    }
    capabilities =frozenset( {
        "write_fs",
        "create_file",
    })

    def __init__(
        self,
        workspace: Workspace,
        mutation_queue: FileMutationQueue,
        checkpoint_store: CheckpointStore,
    ) -> None:
        self.workspace = workspace
        self.mutation_queue = mutation_queue
        self.checkpoint_store = checkpoint_store

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:

        path = arguments.get("path")
        content = arguments.get("content")

        create_only = arguments.get(
            "create_only",
            True,
        )

        if not path:
            raise ValueError("缺少 path")

        if content is None:
            raise ValueError("缺少 content")

        if create_only is not True:
            raise ValueError(
                "当前 WriteTool 仅支持 create_only=True"
            )

        file_path = self.workspace.resolve(
            path
        )

        async with self.mutation_queue.hold(file_path):
            if file_path.exists():
                raise FileExistsError(
                    f"文件已存在: {path}。"
                    "请使用 edit 工具修改已有文件。"
                )

            # 文件此时不存在：
            # checkpoint 会记录 existed=False
            checkpoint = self.checkpoint_store.create(
                path=file_path,
                workspace_relative=path,
            )


            new_bytes = encode_text_file(
                content,
                newline="LF",
                has_bom=False,
            )

            atomic_write_bytes(
                file_path,
                new_bytes,
            )

            new_version = version_of_file(
                file_path
            )

            diff = unified_diff(
                "",
                content,
                path,
            )

            mutation = MutationResult(
                path=path,
                old_version=None,
                new_version=new_version,
                diff=diff,
                changed=True,
                checkpoint_id=checkpoint.id,
            )

        return AgentToolResult(
            content=f"成功创建文件 {path}",
            details={
                "mutation": mutation.model_dump(),
            },
        )