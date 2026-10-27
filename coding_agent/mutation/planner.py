"""Workspace mutation planner（纯只读）。

把 WorkspaceChangeSet 规划成 PlannedWorkspaceChangeSet。
任何失败 → 0 filesystem mutation。
"""

from __future__ import annotations

from pathlib import Path

from coding_agent.changes.diff import unified_diff
from coding_agent.changes.ranges import build_changed_ranges
from coding_agent.changes.workspace_ir import (
    CreateFileOp,
    DeleteFileOp,
    EditFileOp,
    MoveFileOp,
    WorkspaceChangeSet,
)
from coding_agent.editing.exact import (
    ExactReplacement,
    apply_planned_replacements,
    plan_exact_replacements,
    validate_no_overlap,
)
from coding_agent.errors import (
    FileAlreadyExistsAgentError,
    FileNotFoundAgentError,
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.snapshots import snapshot_file
from coding_agent.filesystem.versions import same_version
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.models import (
    PlannedCreate,
    PlannedDelete,
    PlannedEdit,
    PlannedMove,
    PlannedOperation,
    PlannedWorkspaceChangeSet,
)


def plan_workspace_change_set(
    workspace: Workspace,
    change_set: WorkspaceChangeSet,
) -> PlannedWorkspaceChangeSet:
    """只读规划。抛出任何异常都保证 0 文件系统修改。"""

    # --------------------------------------------------
    # 1. 冲突检测（在 resolve/snapshot 之前先做保守检查）。
    # --------------------------------------------------

    _detect_conflicts(change_set)

    planned: list[PlannedOperation] = []
    lock_paths: set[str] = set()

    for operation in change_set.operations:
        if isinstance(operation, CreateFileOp):
            planned_op, locks = _plan_create(
                workspace, operation
            )

        elif isinstance(operation, EditFileOp):
            planned_op, locks = _plan_edit(
                workspace, operation
            )

        elif isinstance(operation, DeleteFileOp):
            planned_op, locks = _plan_delete(
                workspace, operation
            )

        elif isinstance(operation, MoveFileOp):
            planned_op, locks = _plan_move(
                workspace, operation
            )

        else:  # pragma: no cover - 防御性
            raise InvalidArgumentError(
                f"未知操作类型: {type(operation)}"
            )

        planned.append(planned_op)
        lock_paths.update(locks)

    return PlannedWorkspaceChangeSet(
        operations=planned,
        lock_paths=sorted(lock_paths),
    )


# ---------------------------------------------------------------------------
# 各操作规划
# ---------------------------------------------------------------------------


def _plan_create(
    workspace: Workspace,
    op: CreateFileOp,
) -> tuple[PlannedCreate, list[str]]:
    file_path = workspace.resolve(op.path)

    if file_path.exists():
        raise FileAlreadyExistsAgentError(op.path)

    # 父目录必须存在（第一版不自动建目录）。
    if not file_path.parent.exists():
        raise InvalidArgumentError(
            f"父目录不存在: {op.path}"
        )

    return (
        PlannedCreate(
            path=op.path,
            content=op.content,
        ),
        [str(file_path)],
    )


def _plan_edit(
    workspace: Workspace,
    op: EditFileOp,
) -> tuple[PlannedEdit, list[str]]:
    file_path = workspace.resolve(op.path)

    if not file_path.exists():
        raise FileNotFoundAgentError(op.path)

    snapshot = snapshot_file(
        file_path,
        workspace_relative=op.path,
    )

    if op.expected_version is not None:
        if not same_version(
            snapshot.version,
            op.expected_version,
        ):
            raise StaleVersionError(
                expected=op.expected_version.digest,
                current=snapshot.version.digest,
            )

    replacements = [
        ExactReplacement(
            old_text=r.old_text,
            new_text=r.new_text,
        )
        for r in op.replacements
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
        op.path,
    )

    changed_ranges = build_changed_ranges(
        path=op.path,
        original=snapshot.content,
        new_content=new_content,
        matches=matches,
    )

    return (
        PlannedEdit(
            path=op.path,
            snapshot=snapshot,
            new_content=new_content,
            diff=diff,
            changed_ranges=changed_ranges,
        ),
        [str(file_path)],
    )


def _plan_delete(
    workspace: Workspace,
    op: DeleteFileOp,
) -> tuple[PlannedDelete, list[str]]:
    file_path = workspace.resolve(op.path)

    if not file_path.exists():
        raise FileNotFoundAgentError(op.path)

    if not file_path.is_file():
        raise InvalidArgumentError(
            f"目标不是普通文件: {op.path}"
        )

    snapshot = snapshot_file(
        file_path,
        workspace_relative=op.path,
    )

    if not same_version(
        snapshot.version,
        op.expected_version,
    ):
        raise StaleVersionError(
            expected=op.expected_version.digest,
            current=snapshot.version.digest,
        )

    return (
        PlannedDelete(
            path=op.path,
            snapshot=snapshot,
        ),
        [str(file_path)],
    )


def _plan_move(
    workspace: Workspace,
    op: MoveFileOp,
) -> tuple[PlannedMove, list[str]]:
    source_path = workspace.resolve(op.source)
    destination_path = workspace.resolve(op.destination)

    if source_path == destination_path:
        raise InvalidArgumentError(
            "source 与 destination 不能相同"
        )

    if not source_path.exists():
        raise FileNotFoundAgentError(op.source)

    if not source_path.is_file():
        raise InvalidArgumentError(
            f"source 不是普通文件: {op.source}"
        )

    if destination_path.exists():
        raise FileAlreadyExistsAgentError(
            op.destination
        )

    snapshot = snapshot_file(
        source_path,
        workspace_relative=op.source,
    )

    if not same_version(
        snapshot.version,
        op.expected_source_version,
    ):
        raise StaleVersionError(
            expected=op.expected_source_version.digest,
            current=snapshot.version.digest,
        )

    return (
        PlannedMove(
            source=op.source,
            destination=op.destination,
            source_snapshot=snapshot,
        ),
        [str(source_path), str(destination_path)],
    )


# ---------------------------------------------------------------------------
# 冲突检测
# ---------------------------------------------------------------------------


def _detect_conflicts(
    change_set: WorkspaceChangeSet,
) -> None:
    """保守拒绝同一路径/目标被多个操作以冲突方式触碰。"""

    edited: set[str] = set()
    deleted: set[str] = set()
    created: set[str] = set()
    move_sources: dict[str, str] = {}
    move_destinations: dict[str, str] = {}
    # 记录每个 move 的 destination，用于检测重复目标。
    move_dest_list: list[str] = []

    for operation in change_set.operations:
        if isinstance(operation, EditFileOp):
            edited.add(operation.path)

        elif isinstance(operation, DeleteFileOp):
            deleted.add(operation.path)

        elif isinstance(operation, CreateFileOp):
            created.add(operation.path)

        elif isinstance(operation, MoveFileOp):
            move_sources[operation.source] = operation.destination
            move_destinations[operation.destination] = operation.source
            move_dest_list.append(operation.destination)

    # Edit A + Delete A
    for path in edited & deleted:
        raise InvalidArgumentError(
            f"同一文件同时 edit 与 delete: {path}"
        )

    # Delete A + Move A→B
    for path in deleted & set(move_sources):
        raise InvalidArgumentError(
            f"同一文件同时 delete 与 move: {path}"
        )

    # Create B + Move A→B（move 目标被 create 占用）
    for path in created & set(move_destinations):
        raise InvalidArgumentError(
            f"move 目标被 create 占用: {path}"
        )

    # Move A→B + Move C→B（多个 move 指向同一目标）
    if len(move_dest_list) != len(set(move_dest_list)):
        raise InvalidArgumentError(
            "多个 move 指向同一目标"
        )

    # Move A→B + Edit B（第一版保守拒绝，除非实现 dependency DAG）
    for destination in move_destinations:
        if destination in edited:
            raise InvalidArgumentError(
                f"move 目标又被 edit: {destination}"
            )

    # Move A→B + Edit A（move source 又被 edit）
    for source in move_sources:
        if source in edited:
            raise InvalidArgumentError(
                f"move source 又被 edit: {source}"
            )
