"""Patch runtime: plan a multi-file ChangeSet, then apply safely.

与 EditTool 的区别：

    EditTool = 单文件直接 exact edit
    Patch     = ChangeSet + 多文件 plan / apply

第一版仍是 deterministic exact replacement，不做 fuzzy matching。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

from pydantic import BaseModel, Field

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.diff import unified_diff
from coding_agent.changes.ir import ChangeSet, FileChange
from coding_agent.changes.models import ChangedRange, MutationResult
from coding_agent.changes.ranges import build_changed_ranges
from coding_agent.changes.rollback import RollbackManager
from coding_agent.editing.exact import (
    ExactReplacement,
    apply_planned_replacements,
    plan_exact_replacements,
    validate_no_overlap,
)
from coding_agent.errors import (
    FileNotFoundAgentError,
    InvalidArgumentError,
    PatchRecoveryError,
    StaleVersionError,
)
from coding_agent.filesystem.atomic_write import (
    atomic_write_bytes_checked,
)
from coding_agent.filesystem.encoding import encode_text_file
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.snapshots import (
    FileSnapshot,
    snapshot_file,
)
from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
    version_of_file,
)
from coding_agent.filesystem.workspace import Workspace

# Blast radius 边界（V3 第一版保守限制）。
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_REPLACEMENTS_PER_FILE = 50

# PatchTool 输出 diff 的最大字节数。
DEFAULT_MAX_DIFF_BYTES = 10_000


class PlannedFileChange(BaseModel):
    """plan 阶段产出：每个文件的新内容与 diff，尚未落盘。"""

    path: str
    snapshot: FileSnapshot
    new_content: str
    diff: str
    changed_ranges: list[ChangedRange]


class PlannedChangeSet(BaseModel):
    files: list[PlannedFileChange]


class PatchResult(BaseModel):
    """Patch apply 的成功结果。"""

    files: list[str]
    mutations: list[MutationResult]
    diff: str = ""
    checkpoint_ids: list[str] = Field(default_factory=list)


def plan_change_set(
    workspace: Workspace,
    change_set: ChangeSet,
) -> PlannedChangeSet:
    """对整个 ChangeSet 做 plan：任何一步失败都不落盘。

    流程：
        resolve all paths
        → snapshot all
        → version check all
        → plan matches all
        → overlap check all
        → generate all new content

    只读，不产生任何磁盘修改。
    """

    planned: list[PlannedFileChange] = []

    for file_change in change_set.files:
        file_path = workspace.resolve(file_change.path)

        if not file_path.exists():
            raise FileNotFoundAgentError(file_change.path)

        # --------------------------------------------------
        # snapshot
        # --------------------------------------------------

        snapshot = snapshot_file(
            file_path,
            workspace_relative=file_change.path,
        )

        # --------------------------------------------------
        # stale version 检查
        # --------------------------------------------------

        if file_change.expected_version is not None:
            if not same_version(
                snapshot.version,
                file_change.expected_version,
            ):
                raise StaleVersionError(
                    expected=file_change.expected_version.digest,
                    current=snapshot.version.digest,
                )

        # --------------------------------------------------
        # plan replacements + overlap check
        # --------------------------------------------------

        replacements = [
            ExactReplacement(
                old_text=r.old_text,
                new_text=r.new_text,
            )
            for r in file_change.replacements
        ]

        matches = plan_exact_replacements(
            snapshot.content,
            replacements,
        )

        validate_no_overlap(matches)

        new_content = apply_planned_replacements(
            snapshot.content,
            matches,
        )

        diff = unified_diff(
            snapshot.content,
            new_content,
            file_change.path,
        )

        changed_ranges = build_changed_ranges(
            path=file_change.path,
            original=snapshot.content,
            new_content=new_content,
            matches=matches,
        )

        planned.append(
            PlannedFileChange(
                path=file_change.path,
                snapshot=snapshot,
                new_content=new_content,
                diff=diff,
                changed_ranges=changed_ranges,
            )
        )

    return PlannedChangeSet(files=planned)


def ordered_lock_paths(
    workspace: Workspace,
    change_set: ChangeSet,
) -> list[Path]:
    """返回按 canonical resolved path 稳定排序的去重 lock 路径。

    用于避免多文件并发死锁：所有调用方必须按同一顺序加锁。
    """

    keys = sorted(
        {
            workspace.resolve(fc.path)
            for fc in change_set.files
        },
        key=lambda p: str(p),
    )

    return keys


class PatchRuntime:
    """对 ChangeSet 做 plan-then-apply，含 partial-failure rollback。"""

    def __init__(
        self,
        workspace: Workspace,
        mutation_queue: FileMutationQueue,
        checkpoint_store: CheckpointStore,
    ) -> None:
        self.workspace = workspace
        self.mutation_queue = mutation_queue
        self.checkpoint_store = checkpoint_store
        self.rollback_manager = RollbackManager(
            workspace,
            mutation_queue,
            checkpoint_store,
        )

    async def apply(
        self,
        change_set: ChangeSet,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_replacements_per_file: int = DEFAULT_MAX_REPLACEMENTS_PER_FILE,
    ) -> PatchResult:
        self._validate_limits(
            change_set,
            max_files,
            max_replacements_per_file,
        )

        # --------------------------------------------------
        # 1. plan 全部：任何失败 → 0 files changed（尚未写盘）。
        # --------------------------------------------------

        planned = plan_change_set(
            self.workspace,
            change_set,
        )

        ordered = planned.files

        written: list[
            tuple[PlannedFileChange, str, FileVersion]
        ] = []

        try:
            async with AsyncExitStack() as stack:
                # ----------------------------------------------
                # 2. 按稳定顺序获取所有 mutation lock。
                # ----------------------------------------------

                for pf in ordered:
                    await stack.enter_async_context(
                        self.mutation_queue.hold(
                            self.workspace.resolve(pf.path)
                        )
                    )

                # ----------------------------------------------
                # 3. 重新检查版本（plan 与 apply 之间可能变化）。
                # ----------------------------------------------

                for pf in ordered:
                    current = version_of_file(
                        self.workspace.resolve(pf.path)
                    )

                    if not same_version(
                        current,
                        pf.snapshot.version,
                    ):
                        raise StaleVersionError(
                            expected=pf.snapshot.version.digest,
                            current=current.digest,
                        )

                # ----------------------------------------------
                # 4. create checkpoints
                # ----------------------------------------------

                checkpoints: dict[str, str] = {}

                for pf in ordered:
                    checkpoint = self.checkpoint_store.create(
                        path=self.workspace.resolve(pf.path),
                        workspace_relative=pf.path,
                    )
                    checkpoints[pf.path] = checkpoint.id

                # ----------------------------------------------
                # 5. atomic write each file
                # ----------------------------------------------

                for pf in ordered:
                    file_path = self.workspace.resolve(pf.path)

                    new_bytes = encode_text_file(
                        pf.new_content,
                        newline=pf.snapshot.newline,
                        has_bom=pf.snapshot.has_bom,
                    )

                    atomic_write_bytes_checked(
                        file_path,
                        new_bytes,
                        expected_version=pf.snapshot.version,
                    )

                    new_version = version_of_file(
                        file_path
                    )

                    written.append(
                        (
                            pf,
                            checkpoints[pf.path],
                            new_version,
                        )
                    )

        except Exception as exc:
            # ----------------------------------------------
            # 6. partial failure → best-effort rollback。
            #
            #    此处的 Exception 已退出 AsyncExitStack，
            #    所有 lock 已释放，可以安全调用 rollback。
            # ----------------------------------------------

            unrecovered = await self._rollback(written)

            if unrecovered:
                raise PatchRecoveryError(
                    unrecovered_files=unrecovered,
                    cause=str(exc),
                ) from exc

            raise

        # --------------------------------------------------
        # 7. 构建结果。
        # --------------------------------------------------

        mutations = [
            MutationResult(
                path=pf.path,
                old_version=pf.snapshot.version,
                new_version=new_version,
                diff=pf.diff,
                changed_ranges=pf.changed_ranges,
                checkpoint_id=checkpoint_id,
                changed=True,
            )
            for pf, checkpoint_id, new_version in written
        ]

        return PatchResult(
            files=[pf.path for pf in ordered],
            mutations=mutations,
            diff=self._combined_diff(mutations),
            checkpoint_ids=[
                cid for _, cid, _ in written
            ],
        )

    async def _rollback(
        self,
        written: list[
            tuple[PlannedFileChange, str, FileVersion]
        ],
    ) -> list[str]:
        """逆序回滚已写文件，best-effort。

        Returns:
            未能成功回滚（recovery 失败）的文件 path 列表。
        """

        unrecovered: list[str] = []

        for pf, checkpoint_id, new_version in reversed(written):
            try:
                await self.rollback_manager.rollback(
                    checkpoint_id,
                    expected_current_version=new_version,
                )
            except Exception:
                # best-effort：单个回滚失败不阻断其余，
                # 但必须上报未恢复文件。
                unrecovered.append(pf.path)

        return unrecovered

    def _combined_diff(
        self,
        mutations: list[MutationResult],
    ) -> str:
        combined = "\n".join(
            m.diff for m in mutations if m.diff
        )

        if len(combined.encode("utf-8")) > DEFAULT_MAX_DIFF_BYTES:
            return (
                combined[:DEFAULT_MAX_DIFF_BYTES]
                + "\n...[diff truncated]...\n"
            )

        return combined

    @staticmethod
    def _validate_limits(
        change_set: ChangeSet,
        max_files: int,
        max_replacements_per_file: int,
    ) -> None:
        if not change_set.files:
            raise InvalidArgumentError(
                "ChangeSet 不能为空"
            )

        if len(change_set.files) > max_files:
            raise InvalidArgumentError(
                f"文件数超过上限 {max_files}"
            )

        for fc in change_set.files:
            if not fc.path:
                raise InvalidArgumentError(
                    "path 不能为空"
                )

            if not fc.replacements:
                raise InvalidArgumentError(
                    f"{fc.path} 至少需要一个 replacement"
                )

            if len(fc.replacements) > max_replacements_per_file:
                raise InvalidArgumentError(
                    f"{fc.path} replacement 数超过上限 "
                    f"{max_replacements_per_file}"
                )
