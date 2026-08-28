"""Single-file rollback manager."""

from __future__ import annotations

from coding_agent.changes.checkpoint import (
    CheckpointStore,
)
from coding_agent.changes.diff import unified_diff
from coding_agent.changes.models import MutationResult
from coding_agent.errors import (
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.atomic_write import (
    atomic_write_bytes_checked,
)
from coding_agent.filesystem.encoding import (
    decode_text_file,
)
from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)
from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
    version_of_file,
)
from coding_agent.filesystem.workspace import Workspace


class RollbackManager:
    """负责安全恢复单文件 checkpoint。"""

    def __init__(
        self,
        workspace: Workspace,
        mutation_queue: FileMutationQueue,
        checkpoint_store: CheckpointStore,
    ) -> None:
        self.workspace = workspace
        self.mutation_queue = mutation_queue
        self.checkpoint_store = checkpoint_store

    async def rollback(
        self,
        checkpoint_id: str,
        *,
        expected_current_version: FileVersion,
    ) -> MutationResult:
        """恢复一个 checkpoint。

        expected_current_version:
            调用 rollback 时，调用方认为磁盘当前应该处于的版本。

            通常传入原 mutation 的 new_version。

        如果磁盘已经再次发生变化，则拒绝 rollback，
        防止覆盖用户在 mutation 后的新修改。
        """

        checkpoint = self.checkpoint_store.get(
            checkpoint_id
        )

        if checkpoint is None:
            raise InvalidArgumentError(
                f"Checkpoint 不存在: {checkpoint_id}"
            )

        file_path = self.workspace.resolve(
            checkpoint.path
        )

        async with self.mutation_queue.hold(
            file_path
        ):
            # ==========================================
            # Case A:
            # checkpoint 前文件本来存在
            #
            # EditTool rollback：
            # 当前 H2 → 恢复原来的 H1
            # ==========================================

            if checkpoint.existed:
                if not file_path.exists():
                    raise StaleVersionError(
                        expected=expected_current_version.digest,
                        current="<missing>",
                    )

                current_version = version_of_file(
                    file_path
                )

                if not same_version(
                    current_version,
                    expected_current_version,
                ):
                    raise StaleVersionError(
                        expected=expected_current_version.digest,
                        current=current_version.digest,
                    )

                assert checkpoint.content is not None
                assert checkpoint.version is not None

                # 用于生成 rollback diff
                current_bytes = file_path.read_bytes()

                current_text = decode_text_file(
                    current_bytes
                ).content

                restored_text = decode_text_file(
                    checkpoint.content
                ).content

                diff = unified_diff(
                    current_text,
                    restored_text,
                    checkpoint.path,
                )

                # atomic writer 内还会再做一次版本检查，
                # 防止检查之后、replace 之前发生外部修改。
                atomic_write_bytes_checked(
                    file_path,
                    checkpoint.content,
                    expected_version=current_version,
                )

                restored_version = version_of_file(
                    file_path
                )

                return MutationResult(
                    path=checkpoint.path,

                    # rollback 前的 H2
                    old_version=current_version,

                    # rollback 后恢复为 H1
                    new_version=restored_version,

                    diff=diff,

                    changed=True,
                )

            # ==========================================
            # Case B:
            # checkpoint 前文件本来不存在
            #
            # WriteTool rollback：
            # 删除 Agent 刚创建的文件
            # ==========================================

            if not file_path.exists():
                raise StaleVersionError(
                    expected=expected_current_version.digest,
                    current="<missing>",
                )

            current_version = version_of_file(
                file_path
            )

            if not same_version(
                current_version,
                expected_current_version,
            ):
                raise StaleVersionError(
                    expected=expected_current_version.digest,
                    current=current_version.digest,
                )

            current_bytes = file_path.read_bytes()

            current_text = decode_text_file(
                current_bytes
            ).content

            diff = unified_diff(
                current_text,
                "",
                checkpoint.path,
            )

            # 第一版：
            # queue + version check 后删除。
            #
            # 后面可以继续升级成 delete_file_checked()
            # 来缩小外部进程 race window。
            file_path.unlink()

            return MutationResult(
                path=checkpoint.path,
                old_version=current_version,
                new_version=None,
                diff=diff,
                changed=True,
            )