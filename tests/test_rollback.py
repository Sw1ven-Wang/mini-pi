"""Tests for single-file checkpoint rollback.

覆盖：

1. Edit rollback：恢复原始 bytes（UTF-8 BOM + CRLF 完全保真）。
2. Write rollback：删除新建的文件。
3. stale protection：磁盘已再变化时拒绝 rollback，且不覆盖用户修改。
4. 未知 checkpoint。
5. rollback 两次：第二次不能悄悄成功。
"""

import asyncio
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.rollback import RollbackManager
from coding_agent.errors import (
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)
from coding_agent.filesystem.versions import (
    version_of_file,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.tools.edit import EditTool
from coding_agent.tools.write import WriteTool


def _run(coro):
    """运行 async coroutine。"""
    return asyncio.run(coro)


def _runtime(tmp_path: Path):
    """构建 EditTool / WriteTool / RollbackManager，共享同一套 runtime。"""
    workspace = Workspace(tmp_path)
    queue = FileMutationQueue()
    store = CheckpointStore()

    edit = EditTool(workspace, queue, store)
    write = WriteTool(workspace, queue, store)
    rollback = RollbackManager(workspace, queue, store)

    return edit, write, rollback


# ---------------------------------------------------------------------------
# 1. Edit rollback 恢复原始 bytes
# ---------------------------------------------------------------------------


def test_edit_then_rollback_restores_exact_original_bytes(
    tmp_path: Path,
):
    edit, _, rollback = _runtime(tmp_path)

    file_path = tmp_path / "main.py"

    # 带 UTF-8 BOM + CRLF 的原始文件
    original_bytes = b"\xef\xbb\xbfline1\r\nline2\r\n"
    file_path.write_bytes(original_bytes)

    result = _run(
        edit.execute(
            "call_1",
            {
                "path": "main.py",
                "edits": [
                    {"old_text": "line1", "new_text": "LINE1"},
                ],
            },
        )
    )

    checkpoint_id = result.details["mutation"]["checkpoint_id"]
    new_version = version_of_file(file_path)  # H2

    _run(
        rollback.rollback(
            checkpoint_id,
            expected_current_version=new_version,
        )
    )

    # 必须 bytes 完全一致（BOM / CRLF 原样恢复）
    assert file_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# 2. Write rollback 删除新建文件
# ---------------------------------------------------------------------------


def test_write_then_rollback_removes_created_file(
    tmp_path: Path,
):
    _, write, rollback = _runtime(tmp_path)

    file_path = tmp_path / "new.txt"

    result = _run(
        write.execute(
            "call_1",
            {
                "path": "new.txt",
                "content": "hello",
            },
        )
    )

    checkpoint_id = result.details["mutation"]["checkpoint_id"]
    new_version = version_of_file(file_path)  # H1

    assert file_path.exists()

    _run(
        rollback.rollback(
            checkpoint_id,
            expected_current_version=new_version,
        )
    )

    assert not file_path.exists()


# ---------------------------------------------------------------------------
# 3. stale protection
# ---------------------------------------------------------------------------


def test_rollback_rejects_user_change_after_edit(
    tmp_path: Path,
):
    edit, _, rollback = _runtime(tmp_path)

    file_path = tmp_path / "main.py"
    file_path.write_text("line1\n", encoding="utf-8")

    result = _run(
        edit.execute(
            "call_1",
            {
                "path": "main.py",
                "edits": [
                    {"old_text": "line1", "new_text": "LINE1"},
                ],
            },
        )
    )

    checkpoint_id = result.details["mutation"]["checkpoint_id"]
    h2 = version_of_file(file_path)  # H2

    # 用户在 Agent 修改之后，又改了文件 → H3
    user_bytes = b"user changed this\n"
    file_path.write_bytes(user_bytes)

    with pytest.raises(StaleVersionError):
        _run(
            rollback.rollback(
                checkpoint_id,
                expected_current_version=h2,
            )
        )

    # 用户的修改必须原样保留，不能被 rollback 覆盖
    assert file_path.read_bytes() == user_bytes


# ---------------------------------------------------------------------------
# 4. 未知 checkpoint
# ---------------------------------------------------------------------------


def test_rollback_unknown_checkpoint(
    tmp_path: Path,
):
    _, _, rollback = _runtime(tmp_path)

    file_path = tmp_path / "x.txt"
    file_path.write_text("x", encoding="utf-8")
    expected = version_of_file(file_path)

    with pytest.raises(InvalidArgumentError):
        _run(
            rollback.rollback(
                "does-not-exist",
                expected_current_version=expected,
            )
        )


# ---------------------------------------------------------------------------
# 5. rollback 两次
# ---------------------------------------------------------------------------


def test_rollback_twice_reports_stale(
    tmp_path: Path,
):
    edit, _, rollback = _runtime(tmp_path)

    file_path = tmp_path / "main.py"
    file_path.write_text("line1\n", encoding="utf-8")

    result = _run(
        edit.execute(
            "call_1",
            {
                "path": "main.py",
                "edits": [
                    {"old_text": "line1", "new_text": "LINE1"},
                ],
            },
        )
    )

    checkpoint_id = result.details["mutation"]["checkpoint_id"]
    h2 = version_of_file(file_path)

    # 第一次成功
    _run(
        rollback.rollback(
            checkpoint_id,
            expected_current_version=h2,
        )
    )

    # 第二次：文件已回到 H1，expected 仍是 H2 → StaleVersionError
    with pytest.raises(StaleVersionError):
        _run(
            rollback.rollback(
                checkpoint_id,
                expected_current_version=h2,
            )
        )
