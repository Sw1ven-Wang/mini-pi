"""Delete tool: 删除一个文件，走 mutation safety。"""

from __future__ import annotations

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.models import MutationResult
from coding_agent.filesystem.checked_delete import (
    delete_file_checked,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import FileVersion
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import (
    VersionResolver,
    resolve_tool_version,
)


class DeleteTool(AgentTool):
    name = "delete"

    description = (
        "删除 workspace 内的一个普通文件。"
        "文件版本由 Host 管理，删除前应先 read。"
        "不要使用 PowerShell Remove-Item 替代。"
    )

    capabilities = frozenset({
        "write_fs",
        "delete_file",
    })

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要删除的 workspace 相对文件路径",
            },
        },
        "required": [
            "path",
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
        path = arguments.get("path")

        if not path:
            raise ValueError("缺少 path")

        # 版本来源：优先显式 expected_version（兼容），
        # 否则从 ObservationStore 解析。
        expected_version = self._resolve_version(
            arguments,
            path,
        )

        file_path = self.workspace.resolve(path)

        async with self.mutation_queue.hold(file_path):
            checkpoint = self.checkpoint_store.create(
                path=file_path,
                workspace_relative=path,
            )

            old_version = delete_file_checked(
                file_path,
                expected_version=expected_version,
            )

            mutation = MutationResult(
                path=path,
                old_version=old_version,
                new_version=None,
                diff="",
                changed=True,
                checkpoint_id=checkpoint.id,
            )

        return AgentToolResult(
            content=f"已删除文件 {path}",
            details={
                "mutation": mutation.model_dump(),
                "checkpoint_id": checkpoint.id,
            },
        )

    def _resolve_version(
        self,
        arguments: dict[str, Any],
        path: str,
    ) -> FileVersion:
        """解析删除所需的 expected_version。

        优先显式传入的 expected_version（兼容旧 schema），
        否则从 VersionResolver 获取观察版本。
        """

        version = resolve_tool_version(
            path=path,
            explicit_version=arguments.get("expected_version"),
            resolver=self.version_resolver,
            argument_name="expected_version",
        )
        assert version is not None
        return version
