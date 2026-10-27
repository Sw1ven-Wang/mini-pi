"""Tests for workspace mutation planner + runtime."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import (
    CreateFileOp,
    DeleteFileOp,
    EditFileOp,
    MoveFileOp,
    TextReplacement,
    WorkspaceChangeSet,
)
from coding_agent.errors import (
    InvalidArgumentError,
    WorkspaceRecoveryError,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.planner import (
    plan_workspace_change_set,
)
from coding_agent.mutation.runtime import (
    WorkspaceMutationRuntime,
)


def _run(coro):
    return asyncio.run(coro)


def _runtime(tmp_path: Path) -> WorkspaceMutationRuntime:
    return WorkspaceMutationRuntime(
        Workspace(tmp_path),
        FileMutationQueue(),
        CheckpointStore(),
    )


def _v(digest="abc", size=10):
    from coding_agent.filesystem.versions import FileVersion

    return FileVersion(digest=digest, size_bytes=size)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def test_plan_all_valid(tmp_path: Path):
    (tmp_path / "edit.py").write_text("foo\n")
    (tmp_path / "del.py").write_text("del\n")
    (tmp_path / "mv.py").write_text("mv\n")

    workspace = Workspace(tmp_path)

    change_set = WorkspaceChangeSet(
        operations=[
            CreateFileOp(path="new.py", content="n = 1\n"),
            EditFileOp(
                path="edit.py",
                replacements=[
                    TextReplacement(
                        old_text="foo", new_text="bar"
                    )
                ],
            ),
            DeleteFileOp(
                path="del.py",
                expected_version=version_of_file(
                    tmp_path / "del.py"
                ),
            ),
            MoveFileOp(
                source="mv.py",
                destination="moved.py",
                expected_source_version=version_of_file(
                    tmp_path / "mv.py"
                ),
            ),
        ]
    )

    planned = plan_workspace_change_set(workspace, change_set)

    assert len(planned.operations) == 4
    # lock_paths 排序去重。
    assert planned.lock_paths == sorted(planned.lock_paths)
    assert len(set(planned.lock_paths)) == len(planned.lock_paths)


def test_plan_failure_zero_side_effect(tmp_path: Path):
    (tmp_path / "edit.py").write_text("foo\n")

    workspace = Workspace(tmp_path)

    change_set = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="edit.py",
                replacements=[
                    TextReplacement(
                        old_text="not_there", new_text="x"
                    )
                ],
            ),
        ]
    )

    from coding_agent.errors import NoMatchError

    with pytest.raises(NoMatchError):
        plan_workspace_change_set(workspace, change_set)

    assert (tmp_path / "edit.py").read_text() == "foo\n"


def test_conflict_edit_delete(tmp_path: Path):
    change_set = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="a.py",
                replacements=[
                    TextReplacement(old_text="x", new_text="y")
                ],
            ),
            DeleteFileOp(
                path="a.py",
                expected_version=_v(),
            ),
        ]
    )

    with pytest.raises(InvalidArgumentError):
        _detect_conflicts(change_set)


def test_conflict_delete_move(tmp_path: Path):
    change_set = WorkspaceChangeSet(
        operations=[
            DeleteFileOp(
                path="a.py",
                expected_version=_v(),
            ),
            MoveFileOp(
                source="a.py",
                destination="b.py",
                expected_source_version=_v(),
            ),
        ]
    )

    with pytest.raises(InvalidArgumentError):
        _detect_conflicts(change_set)


def test_conflict_create_move_target(tmp_path: Path):
    change_set = WorkspaceChangeSet(
        operations=[
            CreateFileOp(path="b.py", content="x"),
            MoveFileOp(
                source="a.py",
                destination="b.py",
                expected_source_version=_v(),
            ),
        ]
    )

    with pytest.raises(InvalidArgumentError):
        _detect_conflicts(change_set)


def test_conflict_two_moves_same_target(tmp_path: Path):
    change_set = WorkspaceChangeSet(
        operations=[
            MoveFileOp(
                source="a.py",
                destination="c.py",
                expected_source_version=_v(),
            ),
            MoveFileOp(
                source="b.py",
                destination="c.py",
                expected_source_version=_v(),
            ),
        ]
    )

    with pytest.raises(InvalidArgumentError):
        _detect_conflicts(change_set)


def _detect_conflicts(change_set):
    from coding_agent.mutation.planner import _detect_conflicts

    _detect_conflicts(change_set)


def test_lock_paths_sorted_unique(tmp_path: Path):
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.py").write_text("b")

    workspace = Workspace(tmp_path)

    change_set = WorkspaceChangeSet(
        operations=[
            MoveFileOp(
                source="b.py",
                destination="c.py",
                expected_source_version=version_of_file(
                    tmp_path / "b.py"
                ),
            ),
            EditFileOp(
                path="a.py",
                replacements=[
                    TextReplacement(old_text="a", new_text="aa")
                ],
            ),
        ]
    )

    planned = plan_workspace_change_set(workspace, change_set)

    # move 产生两个 lock path，edit 产生一个，全部去重排序。
    assert planned.lock_paths == sorted(planned.lock_paths)
    assert len(planned.lock_paths) == len(set(planned.lock_paths))


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def test_runtime_create_edit_delete_move(tmp_path: Path):
    (tmp_path / "edit.py").write_text("foo\n")
    (tmp_path / "del.py").write_text("del\n")
    (tmp_path / "mv.py").write_text("mv\n")

    runtime = _runtime(tmp_path)

    change_set = WorkspaceChangeSet(
        operations=[
            CreateFileOp(path="new.py", content="n = 1\n"),
            EditFileOp(
                path="edit.py",
                replacements=[
                    TextReplacement(
                        old_text="foo", new_text="bar"
                    )
                ],
            ),
            DeleteFileOp(
                path="del.py",
                expected_version=version_of_file(
                    tmp_path / "del.py"
                ),
            ),
            MoveFileOp(
                source="mv.py",
                destination="moved.py",
                expected_source_version=version_of_file(
                    tmp_path / "mv.py"
                ),
            ),
        ]
    )

    result = _run(runtime.apply(change_set))

    assert len(result.mutations) == 4

    assert (tmp_path / "new.py").read_text() == "n = 1\n"
    assert (tmp_path / "edit.py").read_text() == "bar\n"
    assert not (tmp_path / "del.py").exists()
    assert not (tmp_path / "mv.py").exists()
    assert (tmp_path / "moved.py").read_text() == "mv\n"

    # changed_files 包含 move 的 source 与 destination。
    assert "mv.py" in result.changed_files
    assert "moved.py" in result.changed_files


def test_runtime_final_version_recheck(tmp_path: Path):
    (tmp_path / "edit.py").write_text("foo\n")

    runtime = _runtime(tmp_path)

    version = version_of_file(tmp_path / "edit.py")

    # 外部在 plan 前已修改（stale）。
    (tmp_path / "edit.py").write_text("changed\n")

    change_set = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="edit.py",
                expected_version=version,
                replacements=[
                    TextReplacement(
                        old_text="foo", new_text="bar"
                    )
                ],
            ),
        ]
    )

    from coding_agent.errors import StaleVersionError

    with pytest.raises(StaleVersionError):
        _run(runtime.apply(change_set))

    assert (tmp_path / "edit.py").read_text() == "changed\n"


def test_runtime_partial_failure_reverse_recovery(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "edit.py").write_text("foo\n")
    (tmp_path / "mv.py").write_text("mv\n")

    runtime = _runtime(tmp_path)

    original_edit = (tmp_path / "edit.py").read_bytes()

    # 让 move 在 apply 阶段失败（plan 阶段版本正确）。
    import coding_agent.mutation.runtime as rt

    from coding_agent.errors import StaleVersionError

    def failing_move(source, destination, *, expected_source_version):
        raise StaleVersionError(
            expected="apply-fail", current="x"
        )

    monkeypatch.setattr(rt, "move_file_checked", failing_move)

    change_set = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="edit.py",
                replacements=[
                    TextReplacement(
                        old_text="foo", new_text="bar"
                    )
                ],
            ),
            MoveFileOp(
                source="mv.py",
                destination="moved.py",
                expected_source_version=version_of_file(
                    tmp_path / "mv.py"
                ),
            ),
        ]
    )

    with pytest.raises(StaleVersionError):
        _run(runtime.apply(change_set))

    # edit 已应用但 move 失败 → reverse recovery 应恢复 edit。
    assert (tmp_path / "edit.py").read_bytes() == original_edit
    assert (tmp_path / "mv.py").exists()


def test_runtime_recovery_failure_reported(tmp_path: Path, monkeypatch):
    (tmp_path / "edit.py").write_text("foo\n")
    (tmp_path / "mv.py").write_text("mv\n")

    runtime = _runtime(tmp_path)

    # 让 move 在 apply 阶段失败。
    import coding_agent.mutation.runtime as rt

    from coding_agent.errors import StaleVersionError

    def failing_move(source, destination, *, expected_source_version):
        raise StaleVersionError(
            expected="apply-fail", current="x"
        )

    monkeypatch.setattr(rt, "move_file_checked", failing_move)

    # 让 recovery 也失败：monkeypatch rollback_state 抛异常。
    async def failing_rollback(*args, **kwargs):
        raise RuntimeError("回滚失败")

    monkeypatch.setattr(
        runtime.rollback_manager,
        "rollback_state",
        failing_rollback,
    )

    change_set = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="edit.py",
                replacements=[
                    TextReplacement(
                        old_text="foo", new_text="bar"
                    )
                ],
            ),
            MoveFileOp(
                source="mv.py",
                destination="moved.py",
                expected_source_version=version_of_file(
                    tmp_path / "mv.py"
                ),
            ),
        ]
    )

    with pytest.raises(WorkspaceRecoveryError) as exc_info:
        _run(runtime.apply(change_set))

    report = exc_info.value.details["recovery_report"]

    assert report["attempted"] == 1
    assert report["recovered"] == 0
    assert len(report["failures"]) == 1


def test_runtime_concurrent_external_change_not_overwritten(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "edit.py").write_text("foo\n")
    (tmp_path / "mv.py").write_text("mv\n")

    runtime = _runtime(tmp_path)

    # move 失败前，外部修改了 edit.py（模拟并发）。
    import coding_agent.mutation.runtime as rt

    from coding_agent.errors import StaleVersionError

    def failing_move(source, destination, *, expected_source_version):
        # 外部修改 edit.py，模拟锁释放后用户又改了文件。
        (tmp_path / "edit.py").write_text("external\n")
        raise StaleVersionError(
            expected="apply-fail", current="x"
        )

    monkeypatch.setattr(rt, "move_file_checked", failing_move)

    change_set = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="edit.py",
                replacements=[
                    TextReplacement(
                        old_text="foo", new_text="bar"
                    )
                ],
            ),
            MoveFileOp(
                source="mv.py",
                destination="moved.py",
                expected_source_version=version_of_file(
                    tmp_path / "mv.py"
                ),
            ),
        ]
    )

    with pytest.raises(WorkspaceRecoveryError):
        _run(runtime.apply(change_set))

    # 外部修改未被覆盖（recovery stale reject）。
    assert (tmp_path / "edit.py").read_text() == "external\n"
