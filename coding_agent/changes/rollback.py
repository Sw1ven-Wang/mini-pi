"""Single-file rollback manager."""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

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
    atomic_write_bytes,
    atomic_write_bytes_checked,
)
from coding_agent.filesystem.checked_delete import (
    ExpectedFileState,
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
        """恢复一个 checkpoint（兼容 wrapper）。

        expected_current_version:
            调用 rollback 时，调用方认为磁盘当前应该处于的版本。

        等价于 expected_current_state = ExpectedFileState(
            exists=True, version=expected_current_version
        )。
        """

        return await self.rollback_state(
            checkpoint_id,
            expected_current_state=ExpectedFileState(
                exists=True,
                version=expected_current_version,
            ),
        )

    async def rollback_state(
        self,
        checkpoint_id: str,
        *,
        expected_current_state: ExpectedFileState,
    ) -> MutationResult:
        """按 ExpectedFileState 恢复 checkpoint。

        支持三种场景：
            - Edit rollback   ：checkpoint.existed=True，expected exists=True
            - Create rollback ：checkpoint.existed=False，expected exists=True
            - Delete rollback ：checkpoint.existed=True，expected exists=False
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
            # Delete rollback：
            # checkpoint 前文件存在，现在应处于 missing 状态。
            # ==========================================

            if checkpoint.existed and not expected_current_state.exists:
                return self._rollback_delete(
                    checkpoint,
                    file_path,
                )

            # ==========================================
            # Edit rollback：
            # checkpoint 前文件存在，现在也应存在且版本匹配。
            # ==========================================

            if checkpoint.existed:
                return self._rollback_edit(
                    checkpoint,
                    file_path,
                    expected_current_state,
                )

            # ==========================================
            # Create rollback：
            # checkpoint 前文件不存在，现在应存在且版本匹配。
            # ==========================================

            return self._rollback_create(
                checkpoint,
                file_path,
                expected_current_state,
            )

    def _rollback_edit(
        self,
        checkpoint,
        file_path,
        expected_current_state: ExpectedFileState,
    ) -> MutationResult:
        expected_version = expected_current_state.version

        if expected_version is None:
            raise InvalidArgumentError(
                "Edit rollback 需要 expected version"
            )

        if not file_path.exists():
            raise StaleVersionError(
                expected=expected_version.digest,
                current="<missing>",
            )

        current_version = version_of_file(file_path)

        if not same_version(current_version, expected_version):
            raise StaleVersionError(
                expected=expected_version.digest,
                current=current_version.digest,
            )

        assert checkpoint.content is not None
        assert checkpoint.version is not None

        current_bytes = file_path.read_bytes()

        current_text = decode_text_file(current_bytes).content

        restored_text = decode_text_file(
            checkpoint.content
        ).content

        diff = unified_diff(
            current_text,
            restored_text,
            checkpoint.path,
        )

        atomic_write_bytes_checked(
            file_path,
            checkpoint.content,
            expected_version=current_version,
        )

        restored_version = version_of_file(file_path)

        return MutationResult(
            path=checkpoint.path,
            old_version=current_version,
            new_version=restored_version,
            diff=diff,
            changed=True,
        )

    def _rollback_create(
        self,
        checkpoint,
        file_path,
        expected_current_state: ExpectedFileState,
    ) -> MutationResult:
        expected_version = expected_current_state.version

        if expected_version is None:
            raise InvalidArgumentError(
                "Create rollback 需要 expected version"
            )

        if not file_path.exists():
            raise StaleVersionError(
                expected=expected_version.digest,
                current="<missing>",
            )

        current_version = version_of_file(file_path)

        if not same_version(current_version, expected_version):
            raise StaleVersionError(
                expected=expected_version.digest,
                current=current_version.digest,
            )

        current_bytes = file_path.read_bytes()

        current_text = decode_text_file(current_bytes).content

        diff = unified_diff(
            current_text,
            "",
            checkpoint.path,
        )

        file_path.unlink()

        return MutationResult(
            path=checkpoint.path,
            old_version=current_version,
            new_version=None,
            diff=diff,
            changed=True,
        )

    def _rollback_delete(
        self,
        checkpoint,
        file_path,
    ) -> MutationResult:
        """Delete rollback：恢复一个被删除的文件。

        前置：checkpoint.existed=True，且当前 path 应 missing。
        如果用户已重新创建文件，则拒绝，不覆盖。
        """

        if file_path.exists():
            # 用户重新创建了同名文件 → 拒绝，0 bytes changed。
            raise StaleVersionError(
                expected="<missing>",
                current="<recreated>",
            )

        assert checkpoint.content is not None
        assert checkpoint.version is not None

        diff = unified_diff(
            "",
            decode_text_file(checkpoint.content).content,
            checkpoint.path,
        )

        # 恢复原始 raw bytes（BOM/CRLF 保真）。
        atomic_write_bytes(
            file_path,
            checkpoint.content,
        )

        restored_version = version_of_file(file_path)

        return MutationResult(
            path=checkpoint.path,
            old_version=None,
            new_version=restored_version,
            diff=diff,
            changed=True,
        )

    async def rollback_move(
        self,
        record,
    ) -> MutationResult:
        """Move rollback：把 destination 移回 source。

        前置确认：
            - source 仍 missing
            - destination 仍 == moved_version

        以下情况拒绝：
            - 用户在 source 新建了文件
            - 用户修改了 destination
        """

        source_path = self.workspace.resolve(record.source)
        destination_path = self.workspace.resolve(
            record.destination
        )

        # 双路径 lock，按 canonical resolved path 排序。
        ordered = sorted(
            [str(source_path), str(destination_path)]
        )

        async with AsyncExitStack() as stack:
            for path in ordered:
                await stack.enter_async_context(
                    self.mutation_queue.hold(Path(path))
                )

            # source 仍应 missing。
            if source_path.exists():
                raise StaleVersionError(
                    expected="<missing>",
                    current="<recreated>",
                )

            # destination 仍应存在且版本一致。
            if not destination_path.exists():
                raise StaleVersionError(
                    expected=record.moved_version.digest,
                    current="<missing>",
                )

            current_version = version_of_file(destination_path)

            if not same_version(
                current_version,
                record.moved_version,
            ):
                raise StaleVersionError(
                    expected=record.moved_version.digest,
                    current=current_version.digest,
                )

            import os

            os.rename(destination_path, source_path)

            restored_version = version_of_file(source_path)

            return MutationResult(
                path=record.source,
                old_version=None,
                new_version=restored_version,
                diff="",
                changed=True,
            )