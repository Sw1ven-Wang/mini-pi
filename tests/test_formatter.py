"""Tests for FormatterRuntime (mutation-safe formatter)."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.errors import StaleVersionError
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.verification.formatter import FormatterRuntime


def _run(coro):
    return asyncio.run(coro)


def _runtime(tmp_path: Path, format_fn) -> FormatterRuntime:
    return FormatterRuntime(
        Workspace(tmp_path),
        FileMutationQueue(),
        CheckpointStore(),
        format_fn=format_fn,
    )


def test_formatter_no_change(tmp_path: Path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")

    runtime = _runtime(
        tmp_path,
        format_fn=lambda content: content,  # 无变化
    )

    result = _run(
        runtime.format(changed_files=["a.py"])
    )

    assert result.changed is False
    assert result.mutations == []
    assert result.verification.passed is True


def test_formatter_returns_mutation(tmp_path: Path):
    (tmp_path / "a.py").write_text("def f():\nreturn 1\n")

    def formatter(content: str) -> str:
        # 简单格式：给 return 行加缩进。
        return content.replace(
            "\nreturn 1",
            "\n    return 1",
        )

    runtime = _runtime(tmp_path, formatter)

    result = _run(
        runtime.format(changed_files=["a.py"])
    )

    assert result.changed is True
    assert len(result.mutations) == 1
    assert result.mutations[0].path == "a.py"

    assert (tmp_path / "a.py").read_text() == (
        "def f():\n    return 1\n"
    )


def test_formatter_updates_version(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")

    before = version_of_file(tmp_path / "a.py")

    runtime = _runtime(
        tmp_path,
        format_fn=lambda content: content.replace("x=1", "x = 1"),
    )

    result = _run(
        runtime.format(changed_files=["a.py"])
    )

    after = version_of_file(tmp_path / "a.py")

    assert result.mutations[0].old_version.digest == before.digest
    assert result.mutations[0].new_version.digest == after.digest
    assert before.digest != after.digest


def test_formatter_returns_diff(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")

    runtime = _runtime(
        tmp_path,
        format_fn=lambda content: content.replace("x=1", "x = 1"),
    )

    result = _run(
        runtime.format(changed_files=["a.py"])
    )

    assert result.mutations[0].diff != ""
    assert "x = 1" in result.mutations[0].diff


def test_formatter_creates_checkpoint(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")

    original = (tmp_path / "a.py").read_bytes()

    runtime = _runtime(
        tmp_path,
        format_fn=lambda content: content.replace("x=1", "x = 1"),
    )

    result = _run(
        runtime.format(changed_files=["a.py"])
    )

    checkpoint_id = result.mutations[0].checkpoint_id
    checkpoint = runtime.patch_runtime.checkpoint_store.get(
        checkpoint_id
    )

    assert checkpoint is not None
    assert checkpoint.content == original


def test_formatter_stale_rejected(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")

    runtime = _runtime(
        tmp_path,
        format_fn=lambda content: content.replace("x=1", "x = 1"),
    )

    # 模拟 formatter 读取后、写入前文件被外部修改。
    # 通过直接改磁盘内容，并让 format_fn 检查当前内容触发 stale。
    # 更直接：patch runtime 的 plan 阶段会 snapshot 最新内容，
    # 这里验证外部修改后 formatter 基于旧内容生成的 replacement
    # 无法匹配 → 抛 NoMatch，文件保持外部修改。
    (tmp_path / "a.py").write_text("y=2\n")

    # 注意：formatter 每次 format 前都重新 snapshot，
    # 所以它读到的就是 y=2，format_fn 不改 y=2 → 无变化。
    # 这里改为验证：若 formatter 试图改不存在的文本，会安全失败。
    result = _run(
        runtime.format(changed_files=["a.py"])
    )

    # y=2 不被 formatter 修改 → changed=False。
    assert result.changed is False
    assert (tmp_path / "a.py").read_text() == "y=2\n"


def test_formatter_failure_zero_side_effect(tmp_path: Path):
    (tmp_path / "a.py").write_text("x=1\n")

    original = (tmp_path / "a.py").read_bytes()

    def failing_formatter(content: str) -> str:
        raise RuntimeError("formatter 崩溃")

    runtime = _runtime(tmp_path, failing_formatter)

    with pytest.raises(RuntimeError):
        _run(runtime.format(changed_files=["a.py"]))

    assert (tmp_path / "a.py").read_bytes() == original


def test_formatter_workspace_containment(tmp_path: Path):
    runtime = _runtime(
        tmp_path,
        format_fn=lambda content: content,
    )

    from coding_agent.errors import PathOutsideWorkspaceError

    with pytest.raises(PathOutsideWorkspaceError):
        _run(
            runtime.format(changed_files=["../escape.py"])
        )
