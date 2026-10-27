"""Patch tool: apply a multi-file ChangeSet."""

from __future__ import annotations

from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.ir import ChangeSet, FileChange, TextReplacement
from coding_agent.editing.patch import PatchRuntime
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import (
    VersionResolver,
    resolve_tool_version,
)


class PatchTool(AgentTool):
    name = "patch"

    description = (
        "对多个文件执行一致的精确文本替换。"
        "先规划全部修改，全部可应用后才落盘；"
        "任一文件失败则整体回滚。文件版本由 Host 管理，"
        "修改前应先 read 每个文件。"
    )

    capabilities = frozenset({
        "write_fs",
        "edit_file",
        "multi_file_edit",
    })

    parameters = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "workspace 相对文件路径",
                        },
                        "replacements": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_text": {
                                        "type": "string",
                                    },
                                    "new_text": {
                                        "type": "string",
                                    },
                                },
                                "required": [
                                    "old_text",
                                    "new_text",
                                ],
                            },
                        },
                    },
                    "required": [
                        "path",
                        "replacements",
                    ],
                },
            },
        },
        "required": [
            "files",
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
        self.runtime = PatchRuntime(
            workspace,
            mutation_queue,
            checkpoint_store,
        )
        self.version_resolver = version_resolver

    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        raw_files = arguments.get("files")

        change_set = self._build_change_set(raw_files)

        result = await self.runtime.apply(change_set)

        # --------------------------------------------------
        # 简洁文本：只给文件列表，不塞入超长 diff。
        # --------------------------------------------------

        lines = [
            f"Modified {len(result.files)} files:",
            "",
        ]

        for path in result.files:
            lines.append(path)

        return AgentToolResult(
            content="\n".join(lines),
            details={
                "mutations": [
                    m.model_dump()
                    for m in result.mutations
                ],
                "diff": result.diff,
                "checkpoint_ids": result.checkpoint_ids,
            },
        )

    def _build_change_set(
        self,
        raw_files: Any,
    ) -> ChangeSet:
        if not raw_files:
            raise ValueError(
                "至少需要一个 file"
            )

        files: list[FileChange] = []

        for raw in raw_files:
            path = raw.get("path")

            replacements = [
                TextReplacement.model_validate(item)
                for item in raw.get("replacements", [])
            ]

            raw_version = raw.get("expected_version")
            expected_version = resolve_tool_version(
                path=path,
                explicit_version=raw_version,
                resolver=self.version_resolver,
                argument_name="expected_version",
                legacy_optional=True,
            )

            files.append(
                FileChange(
                    path=path,
                    expected_version=expected_version,
                    replacements=replacements,
                )
            )

        return ChangeSet(files=files)
