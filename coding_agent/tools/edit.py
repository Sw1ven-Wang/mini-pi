from typing import Any

from agent.tools import (
    AgentTool,
    AgentToolResult,
)
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.ranges import (
    build_changed_ranges,
)
from coding_agent.changes.diff import unified_diff
from coding_agent.changes.models import MutationResult

from coding_agent.editing.exact import (
    ExactReplacement,
    apply_planned_replacements,
    plan_exact_replacements,
    validate_no_overlap,
)

from coding_agent.filesystem.atomic_write import (
    atomic_write_bytes_checked,
)

from coding_agent.filesystem.encoding import (
    encode_text_file,
)

from coding_agent.filesystem.snapshots import (
    snapshot_file,
)

from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
    version_of_file,
)

from coding_agent.filesystem.workspace import Workspace
from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)

class EditTool(AgentTool):

    name = "edit"

    description = (
        "对同一个文件执行一个或多个精确文本替换。"
        "每个 old_text 必须在原始文件快照中唯一匹配，"
        "多个 edits 不允许重叠。"
    )

    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要修改的 workspace 相对文件路径",
            },
            "edits": {
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
            "expected_version": {
                "type": "object",
                "description": "之前读取文件时获得的版本",
            },
        },
        "required": [
            "path",
            "edits",
        ],
    }
    capabilities =frozenset( {
        "write_fs",
        "edit_file",
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

        # --------------------------------------------------
        # 1. 参数
        # --------------------------------------------------

        path = arguments.get("path")
        raw_edits = arguments.get("edits")

        if not path:
            raise ValueError("缺少 path")

        if not raw_edits:
            raise ValueError("至少需要一个 edit")

        edits = [
            ExactReplacement.model_validate(item)
            for item in raw_edits
        ]

        # --------------------------------------------------
        # 2. Workspace 路径检查
        # --------------------------------------------------

        file_path = self.workspace.resolve(path)

        async with self.mutation_queue.hold(file_path):

            if not file_path.exists():
                raise FileNotFoundError(path)

            # --------------------------------------------------
            # 3. 读取同一个 original snapshot
            # --------------------------------------------------

            snapshot = snapshot_file(
                file_path,
                workspace_relative=path,
            )

            old_version = snapshot.version

            # --------------------------------------------------
            # 4. stale version 检查
            # --------------------------------------------------

            expected_version_data = arguments.get(
                "expected_version"
            )

            if expected_version_data is not None:

                expected_version = FileVersion.model_validate(
                    expected_version_data
                )

                if not same_version(
                    old_version,
                    expected_version,
                ):
                    raise RuntimeError(
                        "文件版本已变化，请重新读取文件"
                    )

            # --------------------------------------------------
            # 5. 所有 edits 基于同一个 original snapshot
            # --------------------------------------------------

            matches = plan_exact_replacements(
                snapshot.content,
                edits,
            )
            validate_no_overlap(
                matches
            )
            checkpoint = self.checkpoint_store.create(
                path=file_path,
                workspace_relative=path,
            )
            new_content = apply_planned_replacements(
                snapshot.content,
                matches,
            )
            changed_ranges = build_changed_ranges(
                path=path,
                original=snapshot.content,
                new_content=new_content,
                matches=matches,
            )

            # --------------------------------------------------
            # 6. 生成 diff
            # --------------------------------------------------

            diff = unified_diff(
                snapshot.content,
                new_content,
                path,
            )

            # --------------------------------------------------
            # 7. 恢复原始 newline / BOM
            # --------------------------------------------------

            new_bytes = encode_text_file(
                new_content,
                newline=snapshot.newline,
                has_bom=snapshot.has_bom,
            )

            # --------------------------------------------------
            # 8. Atomic Write
            # --------------------------------------------------

            atomic_write_bytes_checked(
                file_path,
                new_bytes,
                expected_version=old_version,
            )

            # --------------------------------------------------
            # 9. 获取真正落盘后的版本
            # --------------------------------------------------

            new_version = version_of_file(
                file_path
            )

            # --------------------------------------------------
            # 10. MutationResult
            # --------------------------------------------------

            mutation = MutationResult(
                path=path,
                old_version=old_version,
                new_version=new_version,
                diff=diff,
                changed_ranges=changed_ranges,
                checkpoint_id=checkpoint.id,
                changed=True,
            )

        return AgentToolResult(
            content=(
                f"成功修改文件 {path}，"
                f"共执行 {len(edits)} 个精确替换"
            ),
            details={
                "mutation": mutation.model_dump(),
            },
        )