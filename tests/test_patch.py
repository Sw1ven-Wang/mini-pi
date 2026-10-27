"""Tests for Patch runtime (plan phase + multi-file apply + rollback)."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.ir import ChangeSet, FileChange, TextReplacement
from coding_agent.editing.patch import (
    PatchRuntime,
    plan_change_set,
)
from coding_agent.errors import (
    AmbiguousMatchError,
    NoMatchError,
    StaleVersionError,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace


def _run(coro):
    return asyncio.run(coro)


def _runtime(tmp_path: Path) -> tuple[Workspace, PatchRuntime]:
    workspace = Workspace(tmp_path)
    runtime = PatchRuntime(
        workspace,
        FileMutationQueue(),
        CheckpointStore(),
    )
    return workspace, runtime


def _change_set(*files: FileChange) -> ChangeSet:
    return ChangeSet(files=list(files))


def _repl(old: str, new: str) -> TextReplacement:
    return TextReplacement(old_text=old, new_text=new)


def _file_change(path: str, *replacements: TextReplacement) -> FileChange:
    return FileChange(
        path=path,
        replacements=list(replacements),
    )


# ---------------------------------------------------------------------------
# Plan 阶段
# ---------------------------------------------------------------------------


def test_plan_single_file(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")

    workspace, _ = _runtime(tmp_path)

    planned = plan_change_set(
        workspace,
        _change_set(
            _file_change("a.py", _repl("foo()", "bar()"))
        ),
    )

    assert len(planned.files) == 1
    assert planned.files[0].new_content == "bar()\n"
    assert "foo" in planned.files[0].diff


def test_plan_multiple_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo()\n", encoding="utf-8")

    workspace, _ = _runtime(tmp_path)

    planned = plan_change_set(
        workspace,
        _change_set(
            _file_change("a.py", _repl("foo()", "bar()")),
            _file_change("b.py", _repl("foo()", "bar()")),
        ),
    )

    assert len(planned.files) == 2


def test_plan_failure_changes_zero_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("zzz\n", encoding="utf-8")

    workspace, runtime = _runtime(tmp_path)

    with pytest.raises(NoMatchError):
        _run(
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("foo()", "bar()")),
                    # b.py 无匹配
                    _file_change("b.py", _repl("nothing", "x")),
                )
            )
        )

    # 两个文件都未改变。
    assert (tmp_path / "a.py").read_text() == "foo()\n"
    assert (tmp_path / "b.py").read_text() == "zzz\n"


def test_plan_ambiguous_changes_zero_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\nfoo()\n", encoding="utf-8")

    workspace, runtime = _runtime(tmp_path)

    with pytest.raises(AmbiguousMatchError):
        _run(
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("foo()", "bar()"))
                )
            )
        )

    assert (tmp_path / "a.py").read_text() == "foo()\nfoo()\n"


def test_plan_overlap_changes_zero_files(tmp_path: Path):
    (tmp_path / "a.py").write_text(
        "hello world\n",
        encoding="utf-8",
    )

    workspace, runtime = _runtime(tmp_path)

    with pytest.raises(Exception):
        _run(
            runtime.apply(
                _change_set(
                    _file_change(
                        "a.py",
                        _repl("hello", "HELLO"),
                        _repl("hello world", "bye"),
                    )
                )
            )
        )

    assert (tmp_path / "a.py").read_text() == "hello world\n"


def test_plan_stale_changes_zero_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")

    workspace, runtime = _runtime(tmp_path)

    stale_version = version_of_file(tmp_path / "a.py")

    # 修改文件，使 expected_version 过期。
    (tmp_path / "a.py").write_text("bar()\n", encoding="utf-8")

    with pytest.raises(StaleVersionError):
        _run(
            runtime.apply(
                _change_set(
                    FileChange(
                        path="a.py",
                        expected_version=stale_version,
                        replacements=[_repl("foo()", "baz()")],
                    )
                )
            )
        )

    assert (tmp_path / "a.py").read_text() == "bar()\n"


# ---------------------------------------------------------------------------
# Apply 阶段
# ---------------------------------------------------------------------------


def test_patch_single_file(tmp_path: Path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")

    _, runtime = _runtime(tmp_path)

    result = _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("foo()", "bar()"))
            )
        )
    )

    assert result.files == ["a.py"]
    assert (tmp_path / "a.py").read_text() == "bar()\n"
    assert len(result.mutations) == 1
    assert result.mutations[0].diff != ""
    assert result.mutations[0].changed_ranges != []
    assert len(result.checkpoint_ids) == 1


def test_patch_multiple_files(tmp_path: Path):
    (tmp_path / "a.py").write_text("old_add()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("old_add()\n", encoding="utf-8")

    _, runtime = _runtime(tmp_path)

    result = _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("old_add()", "add()")),
                _file_change("b.py", _repl("old_add()", "add()")),
            )
        )
    )

    assert result.files == ["a.py", "b.py"]
    assert (tmp_path / "a.py").read_text() == "add()\n"
    assert (tmp_path / "b.py").read_text() == "add()\n"


def test_patch_preserves_bom(tmp_path: Path):
    (tmp_path / "a.py").write_bytes(
        b"\xef\xbb\xbf" b"old_add()\n"
    )

    _, runtime = _runtime(tmp_path)

    _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("old_add()", "add()"))
            )
        )
    )

    data = (tmp_path / "a.py").read_bytes()

    assert data.startswith(b"\xef\xbb\xbf")
    assert b"add()" in data


def test_patch_preserves_crlf(tmp_path: Path):
    (tmp_path / "a.py").write_bytes(
        b"old_add()\r\nnext\r\n"
    )

    _, runtime = _runtime(tmp_path)

    _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("old_add()", "add()"))
            )
        )
    )

    data = (tmp_path / "a.py").read_bytes()

    assert b"add()\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")


def test_patch_returns_diff(tmp_path: Path):
    (tmp_path / "a.py").write_text("old_add()\n", encoding="utf-8")

    _, runtime = _runtime(tmp_path)

    result = _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("old_add()", "add()"))
            )
        )
    )

    assert result.diff != ""
    assert "old_add" in result.diff


def test_patch_returns_changed_ranges(tmp_path: Path):
    (tmp_path / "a.py").write_text(
        "old_add()\n",
        encoding="utf-8",
    )

    _, runtime = _runtime(tmp_path)

    result = _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("old_add()", "add()"))
            )
        )
    )

    ranges = result.mutations[0].changed_ranges

    assert len(ranges) == 1
    assert ranges[0].path == "a.py"
    assert ranges[0].old_start_line == 1
    assert ranges[0].new_start_line == 1


def test_patch_creates_checkpoints(tmp_path: Path):
    (tmp_path / "a.py").write_text("old_add()\n", encoding="utf-8")

    original_bytes = (tmp_path / "a.py").read_bytes()

    workspace, runtime = _runtime(tmp_path)
    store = runtime.checkpoint_store

    result = _run(
        runtime.apply(
            _change_set(
                _file_change("a.py", _repl("old_add()", "add()"))
            )
        )
    )

    assert len(result.checkpoint_ids) == 1

    checkpoint = store.get(result.checkpoint_ids[0])
    assert checkpoint is not None
    # checkpoint 保存 mutation 前的真实磁盘 bytes。
    assert checkpoint.content == original_bytes


def test_patch_stale_rejected(tmp_path: Path):
    (tmp_path / "a.py").write_text("old_add()\n", encoding="utf-8")

    _, runtime = _runtime(tmp_path)

    version = version_of_file(tmp_path / "a.py")

    (tmp_path / "a.py").write_text("changed()\n", encoding="utf-8")

    with pytest.raises(StaleVersionError):
        _run(
            runtime.apply(
                _change_set(
                    FileChange(
                        path="a.py",
                        expected_version=version,
                        replacements=[_repl("old_add()", "add()")],
                    )
                )
            )
        )

    assert (tmp_path / "a.py").read_text() == "changed()\n"


def test_patch_partial_failure_rolls_back(tmp_path: Path, monkeypatch):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo()\n", encoding="utf-8")

    original_a = (tmp_path / "a.py").read_bytes()
    original_b = (tmp_path / "b.py").read_bytes()

    workspace, runtime = _runtime(tmp_path)

    # 让 b.py 的原子写入失败，模拟 file1 成功 / file2 失败的场景。
    import coding_agent.editing.patch as patch_module

    real_write = patch_module.atomic_write_bytes_checked

    def failing_write(path, data, *, expected_version):
        if path.name == "b.py":
            raise RuntimeError("模拟写入失败")
        return real_write(path, data, expected_version=expected_version)

    monkeypatch.setattr(
        patch_module,
        "atomic_write_bytes_checked",
        failing_write,
    )

    with pytest.raises(RuntimeError):
        _run(
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("foo()", "bar()")),
                    _file_change("b.py", _repl("foo()", "bar()")),
                )
            )
        )

    # a.py 已被写回原内容（rollback），b.py 未变。
    assert (tmp_path / "a.py").read_bytes() == original_a
    assert (tmp_path / "b.py").read_bytes() == original_b


def test_patch_rollback_rejects_concurrent_external_change(
    tmp_path: Path, monkeypatch
):
    """rollback 必须 stale reject，不能覆盖外部并发修改。"""
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo()\n", encoding="utf-8")

    _, runtime = _runtime(tmp_path)

    import coding_agent.editing.patch as patch_module

    real_write = patch_module.atomic_write_bytes_checked

    def failing_write(path, data, *, expected_version):
        if path.name == "b.py":
            # 模拟外部进程在 rollback 前又修改了 a.py。
            (tmp_path / "a.py").write_text(
                "external edit\n",
                encoding="utf-8",
            )
            raise RuntimeError("模拟写入失败")
        return real_write(path, data, expected_version=expected_version)

    monkeypatch.setattr(
        patch_module,
        "atomic_write_bytes_checked",
        failing_write,
    )

    from coding_agent.errors import PatchRecoveryError

    with pytest.raises(PatchRecoveryError) as exc_info:
        _run(
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("foo()", "bar()")),
                    _file_change("b.py", _repl("foo()", "bar()")),
                )
            )
        )

    # a.py 保留了外部修改，而不是被 rollback 覆盖成旧内容。
    assert (tmp_path / "a.py").read_text() == "external edit\n"

    # 上报了未恢复文件。
    assert "a.py" in exc_info.value.details["unrecovered_files"]


def test_patch_partial_failure_reports_unrecovered_files(
    tmp_path: Path, monkeypatch
):
    """rollback 失败时，必须上报 unrecovered_files。"""
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo()\n", encoding="utf-8")

    _, runtime = _runtime(tmp_path)

    import coding_agent.editing.patch as patch_module

    real_write = patch_module.atomic_write_bytes_checked

    def failing_write(path, data, *, expected_version):
        if path.name == "b.py":
            raise RuntimeError("模拟写入失败")
        return real_write(path, data, expected_version=expected_version)

    monkeypatch.setattr(
        patch_module,
        "atomic_write_bytes_checked",
        failing_write,
    )

    # 让 rollback 也失败，验证 unrecovered 上报。
    async def failing_rollback(checkpoint_id, *, expected_current_version):
        raise RuntimeError("模拟回滚失败")

    monkeypatch.setattr(
        runtime.rollback_manager,
        "rollback",
        failing_rollback,
    )

    from coding_agent.errors import PatchRecoveryError

    with pytest.raises(PatchRecoveryError) as exc_info:
        _run(
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("foo()", "bar()")),
                    _file_change("b.py", _repl("foo()", "bar()")),
                )
            )
        )

    assert exc_info.value.details["unrecovered_files"] == ["a.py"]


# ---------------------------------------------------------------------------
# 多文件加锁
# ---------------------------------------------------------------------------


def test_multi_file_lock_order_is_stable(tmp_path: Path):
    from coding_agent.editing.patch import ordered_lock_paths

    (tmp_path / "z.py").write_text("1")
    (tmp_path / "a.py").write_text("1")

    workspace, _ = _runtime(tmp_path)

    # 传入乱序的 ChangeSet，锁顺序也应稳定排序。
    cs = _change_set(
        _file_change("z.py", _repl("1", "2")),
        _file_change("a.py", _repl("1", "2")),
    )

    ordered = ordered_lock_paths(workspace, cs)

    assert ordered[0].name == "a.py"
    assert ordered[1].name == "z.py"


def test_concurrent_patch_same_files_no_deadlock(tmp_path: Path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

    workspace, _ = _runtime(tmp_path)

    async def patch_twice():
        runtime = PatchRuntime(
            workspace,
            FileMutationQueue(),
            CheckpointStore(),
        )

        await asyncio.gather(
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("x", "y"))
                )
            ),
            runtime.apply(
                _change_set(
                    _file_change("a.py", _repl("y", "z"))
                )
            ),
        )

    asyncio.run(patch_twice())

    # 串行化后最终内容应为 z（第二个 patch 基于第一个的结果）。
    assert (tmp_path / "a.py").read_text() == "z\n"
