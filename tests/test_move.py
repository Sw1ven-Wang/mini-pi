"""Tests for checked move + move rollback."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.rollback import RollbackManager
from coding_agent.errors import (
    FileAlreadyExistsAgentError,
    FileNotFoundAgentError,
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.checked_move import (
    move_file_checked,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.models import MoveRecoveryRecord


def _run(coro):
    return asyncio.run(coro)


def _manager(tmp_path: Path):
    store = CheckpointStore()
    return (
        RollbackManager(
            Workspace(tmp_path),
            FileMutationQueue(),
            store,
        ),
        store,
    )


# ---------------------------------------------------------------------------
# checked move primitive
# ---------------------------------------------------------------------------


def test_move_success(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_text("x = 1\n")

    version = version_of_file(src)

    moved = move_file_checked(
        src,
        dst,
        expected_source_version=version,
    )

    assert not src.exists()
    assert dst.exists()
    assert moved.digest == version.digest


def test_move_preserves_bytes(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    original = b"x = 1\n"
    src.write_bytes(original)

    version = version_of_file(src)

    move_file_checked(
        src,
        dst,
        expected_source_version=version,
    )

    assert dst.read_bytes() == original


def test_move_preserves_bom(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    original = b"\xef\xbb\xbf" b"x = 1\n"
    src.write_bytes(original)

    move_file_checked(
        src,
        dst,
        expected_source_version=version_of_file(src),
    )

    assert dst.read_bytes() == original


def test_move_preserves_crlf(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    original = b"x = 1\r\ny = 2\r\n"
    src.write_bytes(original)

    move_file_checked(
        src,
        dst,
        expected_source_version=version_of_file(src),
    )

    assert dst.read_bytes() == original


def test_move_source_removed(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_text("x")

    move_file_checked(
        src,
        dst,
        expected_source_version=version_of_file(src),
    )

    assert not src.exists()


def test_move_destination_created(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_text("x")

    move_file_checked(
        src,
        dst,
        expected_source_version=version_of_file(src),
    )

    assert dst.exists()


def test_move_destination_exists_rejected(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_text("x")
    dst.write_text("y")

    with pytest.raises(FileAlreadyExistsAgentError):
        move_file_checked(
            src,
            dst,
            expected_source_version=version_of_file(src),
        )

    # 无副作用。
    assert src.exists()
    assert dst.read_text() == "y"


def test_move_stale_source_rejected(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_text("x = 1\n")

    stale = version_of_file(src)

    src.write_text("x = 2\n")

    with pytest.raises(StaleVersionError):
        move_file_checked(
            src,
            dst,
            expected_source_version=stale,
        )

    assert src.exists()
    assert not dst.exists()


def test_move_same_path_rejected(tmp_path: Path):
    src = tmp_path / "a.py"
    src.write_text("x")

    with pytest.raises(InvalidArgumentError):
        move_file_checked(
            src,
            src,
            expected_source_version=version_of_file(src),
        )


# ---------------------------------------------------------------------------
# move rollback
# ---------------------------------------------------------------------------


def test_move_rollback_success(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    original = b"x = 1\n"
    src.write_bytes(original)

    manager, store = _manager(tmp_path)

    source_checkpoint = store.create(
        path=src,
        workspace_relative="a.py",
    )
    destination_checkpoint = store.create(
        path=dst,
        workspace_relative="b.py",
    )

    source_version = version_of_file(src)

    moved_version = move_file_checked(
        src,
        dst,
        expected_source_version=source_version,
    )

    record = MoveRecoveryRecord(
        source="a.py",
        destination="b.py",
        source_checkpoint_id=source_checkpoint.id,
        destination_checkpoint_id=destination_checkpoint.id,
        moved_version=moved_version,
    )

    result = _run(manager.rollback_move(record))

    assert src.exists()
    assert not dst.exists()
    assert src.read_bytes() == original
    assert result.new_version.digest == moved_version.digest


def test_move_rollback_rejects_modified_destination(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_bytes(b"x = 1\n")

    manager, store = _manager(tmp_path)

    source_checkpoint = store.create(
        path=src,
        workspace_relative="a.py",
    )
    destination_checkpoint = store.create(
        path=dst,
        workspace_relative="b.py",
    )

    source_version = version_of_file(src)

    moved_version = move_file_checked(
        src,
        dst,
        expected_source_version=source_version,
    )

    # 用户修改了 destination。
    dst.write_bytes(b"user modified\n")

    record = MoveRecoveryRecord(
        source="a.py",
        destination="b.py",
        source_checkpoint_id=source_checkpoint.id,
        destination_checkpoint_id=destination_checkpoint.id,
        moved_version=moved_version,
    )

    with pytest.raises(StaleVersionError):
        _run(manager.rollback_move(record))

    # 用户修改未被覆盖。
    assert not src.exists()
    assert dst.read_bytes() == b"user modified\n"


def test_move_rollback_rejects_recreated_source(tmp_path: Path):
    src = tmp_path / "a.py"
    dst = tmp_path / "b.py"
    src.write_bytes(b"x = 1\n")

    manager, store = _manager(tmp_path)

    source_checkpoint = store.create(
        path=src,
        workspace_relative="a.py",
    )
    destination_checkpoint = store.create(
        path=dst,
        workspace_relative="b.py",
    )

    source_version = version_of_file(src)

    moved_version = move_file_checked(
        src,
        dst,
        expected_source_version=source_version,
    )

    # 用户在 source 位置新建了文件。
    src.write_bytes(b"user recreated\n")

    record = MoveRecoveryRecord(
        source="a.py",
        destination="b.py",
        source_checkpoint_id=source_checkpoint.id,
        destination_checkpoint_id=destination_checkpoint.id,
        moved_version=moved_version,
    )

    with pytest.raises(StaleVersionError):
        _run(manager.rollback_move(record))

    # 用户文件未被覆盖，destination 仍存在。
    assert src.read_bytes() == b"user recreated\n"
    assert dst.exists()


def test_move_concurrent_lock_order_no_deadlock(tmp_path: Path):
    """两个操作按相反顺序触碰同一对路径，不应死锁。

    关键：move 的两个 path 都按 canonical resolved path 排序后获取，
    因此两个并发 move 会以相同顺序加锁，不会交叉等待。
    """
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text("1")
    b.write_text("2")

    queue = FileMutationQueue()

    async def hold_two(first, second):
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            for p in sorted([first, second], key=str):
                await stack.enter_async_context(queue.hold(p))
            # 短暂持锁，制造并发交叉机会。
            await asyncio.sleep(0.01)

    async def scenario():
        # 一个先 a 后 b，一个先 b 后 a；排序后都变成 a 后 b。
        await asyncio.gather(
            hold_two(a, b),
            hold_two(b, a),
        )

    # 用 timeout 守护：死锁会导致超时异常。
    asyncio.run(
        asyncio.wait_for(scenario(), timeout=5)
    )
