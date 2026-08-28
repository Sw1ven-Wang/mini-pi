"""Tests for checkpoint capture + rollback integration.

这里只验证 Checkpoint 的「捕获」是否正确，
rollback 行为本身已在 tests/test_rollback.py 覆盖。

覆盖：

1. EditTool 的 checkpoint 精确捕获 mutation 前的原始 bytes（BOM + CRLF）。
2. EditTool 的 checkpoint.version 等于 mutation 前的版本。
3. WriteTool 的 checkpoint 记录 existed=False / content=None / version=None。
"""

import asyncio
from pathlib import Path

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)
from coding_agent.filesystem.versions import (
    same_version,
    version_of_file,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.tools.edit import EditTool
from coding_agent.tools.write import WriteTool


def _run(coro):
    """运行 async coroutine。"""
    return asyncio.run(coro)


def _runtime(tmp_path: Path):
    workspace = Workspace(tmp_path)
    queue = FileMutationQueue()
    store = CheckpointStore()

    edit = EditTool(workspace, queue, store)
    write = WriteTool(workspace, queue, store)

    return store, edit, write


def test_edit_checkpoint_captures_exact_original_bytes(
    tmp_path: Path,
):
    store, edit, _ = _runtime(tmp_path)

    file_path = tmp_path / "main.py"
    original_bytes = b"\xef\xbb\xbfx=1\r\ny=2\r\n"
    file_path.write_bytes(original_bytes)

    result = _run(
        edit.execute(
            "call_1",
            {
                "path": "main.py",
                "edits": [
                    {"old_text": "x=1", "new_text": "x=99"},
                ],
            },
        )
    )

    checkpoint = store.get(
        result.details["mutation"]["checkpoint_id"]
    )

    assert checkpoint is not None
    assert checkpoint.existed is True
    assert checkpoint.content == original_bytes
    assert checkpoint.version is not None


def test_edit_checkpoint_version_matches_pre_mutation_version(
    tmp_path: Path,
):
    store, edit, _ = _runtime(tmp_path)

    file_path = tmp_path / "main.py"
    file_path.write_text("hello\n", encoding="utf-8")

    h1 = version_of_file(file_path)

    result = _run(
        edit.execute(
            "call_1",
            {
                "path": "main.py",
                "edits": [
                    {"old_text": "hello", "new_text": "HELLO"},
                ],
            },
        )
    )

    checkpoint = store.get(
        result.details["mutation"]["checkpoint_id"]
    )

    assert checkpoint is not None
    assert checkpoint.version is not None
    assert same_version(checkpoint.version, h1)


def test_write_checkpoint_records_nonexistent_file(
    tmp_path: Path,
):
    store, _, write = _runtime(tmp_path)

    result = _run(
        write.execute(
            "call_1",
            {
                "path": "new.txt",
                "content": "hello",
            },
        )
    )

    checkpoint = store.get(
        result.details["mutation"]["checkpoint_id"]
    )

    assert checkpoint is not None
    assert checkpoint.existed is False
    assert checkpoint.content is None
    assert checkpoint.version is None
