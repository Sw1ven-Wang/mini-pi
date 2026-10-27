"""Move tool: 重命名/移动一个普通文件，走 mutation safety。"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.models import MutationResult
from coding_agent.filesystem.checked_move import (
    move_file_checked,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import (
    VersionResolver,
    resolve_tool_version,
)


class MoveTool(AgentTool):
    name = "move"

    description = (
        "移动或重命名 workspace 内的一个普通文件。"
        "目标路径必须不存在。"
        "文件版本由 Host 管理，移动前应先 read 源文件。"
        "不要使用 PowerShell Move-Item 替代。"
    )

    capabilities = frozenset({
        "write_fs",
        "move_file",
    })

    parameters = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "要移动的源文件路径",
            },
            "destination": {
                "type": "string",
                "description": "目标路径（必须不存在）",
            },
        },
        "required": [
            "source",
            "destination",
        ],
    }

    def __init__(
        self,
        workspace: Workspace,
        mutation_queue: FileMutationQueue,
        checkpoint_store: CheckpointStore,
        *,
        version_resolver: VersionResolver | None = None,
    ) -> None:
        self.workspace = workspace
        self.mutation_queue = mutation_queue
        self.checkpoint_store = checkpoint_store
        self.version_resolver = version_resolver

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        source = arguments.get("source")
        destination = arguments.get("destination")

        if not source:
            raise ValueError("缺少 source")

        if not destination:
            raise ValueError("缺少 destination")

        expected_version = resolve_tool_version(
            path=source,
            explicit_version=arguments.get(
                "expected_source_version"
            ),
            resolver=self.version_resolver,
            argument_name="expected_source_version",
        )
        assert expected_version is not None

        source_path = self.workspace.resolve(source)
        destination_path = self.workspace.resolve(destination)

        # 双路径 lock，按 canonical resolved path 排序。
        ordered = sorted(
            [source_path, destination_path],
            key=lambda p: str(p),
        )

        async with AsyncExitStack() as stack:
            for path in ordered:
                await stack.enter_async_context(
                    self.mutation_queue.hold(path)
                )

            source_checkpoint = self.checkpoint_store.create(
                path=source_path,
                workspace_relative=source,
            )

            destination_checkpoint = self.checkpoint_store.create(
                path=destination_path,
                workspace_relative=destination,
            )

            moved_version = move_file_checked(
                source_path,
                destination_path,
                expected_source_version=expected_version,
            )

            mutation = MutationResult(
                path=destination,
                old_version=expected_version,
                new_version=moved_version,
                diff="",
                changed=True,
                checkpoint_id=source_checkpoint.id,
            )

        return AgentToolResult(
            content=f"已移动 {source} → {destination}",
            details={
                "mutation": mutation.model_dump(),
                "source_checkpoint_id": source_checkpoint.id,
                "destination_checkpoint_id": destination_checkpoint.id,
            },
        )
