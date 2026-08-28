"""Tests for FileMutationQueue."""

import asyncio
from pathlib import Path

from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.tools.edit import EditTool
from pathlib import Path

import pytest

from coding_agent.filesystem.atomic_write import (
    StaleFileError,
    atomic_write_bytes_checked,
)
from coding_agent.filesystem.versions import (
    version_of_file,
)

def test_same_file_is_serialized(
    tmp_path: Path,
):
    queue = FileMutationQueue()

    file_path = tmp_path / "a.txt"

    order: list[str] = []

    async def first():
        async with queue.hold(file_path):
            order.append("first:start")

            await asyncio.sleep(0.05)

            order.append("first:end")

    async def second():
        # 稍微晚一点开始，
        # 确保 first 已经拿到 lock。
        await asyncio.sleep(0.01)

        async with queue.hold(file_path):
            order.append("second:start")
            order.append("second:end")

    async def run():
        await asyncio.gather(
            first(),
            second(),
        )

    asyncio.run(run())

    assert order == [
        "first:start",
        "first:end",
        "second:start",
        "second:end",
    ]
def test_different_files_can_run_in_parallel(
    tmp_path: Path,
):
    queue = FileMutationQueue()

    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"

    order: list[str] = []

    async def mutate_a():
        async with queue.hold(file_a):
            order.append("a:start")

            await asyncio.sleep(0.05)

            order.append("a:end")

    async def mutate_b():
        await asyncio.sleep(0.01)

        async with queue.hold(file_b):
            order.append("b:start")
            order.append("b:end")

    async def run():
        await asyncio.gather(
            mutate_a(),
            mutate_b(),
        )

    asyncio.run(run())

    assert order.index("a:start") < order.index("a:end")
    assert order.index("b:start") < order.index("b:end")

    # b 应该能在 a 结束之前进入
    assert (
        order.index("b:start")
        <
        order.index("a:end")
    )
def test_parallel_edits_same_file(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)
    queue = FileMutationQueue()

    tool = EditTool(
        workspace=workspace,
        mutation_queue=queue,
    )

    file_path = tmp_path / "test.txt"

    file_path.write_text(
        "alpha\nbeta\ngamma\n",
        encoding="utf-8",
    )

    async def run():
        await asyncio.gather(
            tool.execute(
                "call_1",
                {
                    "path": "test.txt",
                    "edits": [
                        {
                            "old_text": "alpha",
                            "new_text": "ALPHA",
                        }
                    ],
                },
            ),
            tool.execute(
                "call_2",
                {
                    "path": "test.txt",
                    "edits": [
                        {
                            "old_text": "beta",
                            "new_text": "BETA",
                        }
                    ],
                },
            ),
        )

    asyncio.run(run())

    assert file_path.read_text(
        encoding="utf-8"
    ) == (
        "ALPHA\n"
        "BETA\n"
        "gamma\n"
    )

def test_atomic_write_checked_success(
    tmp_path: Path,
):
    file_path = tmp_path / "hello.txt"

    file_path.write_bytes(
        b"old"
    )

    expected_version = version_of_file(
        file_path
    )

    atomic_write_bytes_checked(
        file_path,
        b"new",
        expected_version=expected_version,
    )

    assert file_path.read_bytes() == b"new"


def test_atomic_write_checked_rejects_stale_file(
    tmp_path: Path,
):
    file_path = tmp_path / "hello.txt"

    file_path.write_bytes(
        b"original"
    )

    expected_version = version_of_file(
        file_path
    )

    # 模拟 VS Code / Git / 其他进程修改文件
    file_path.write_bytes(
        b"user change"
    )

    with pytest.raises(
        StaleFileError
    ):
        atomic_write_bytes_checked(
            file_path,
            b"agent change",
            expected_version=expected_version,
        )

    # 最关键：
    # Agent 绝不能把用户修改覆盖掉。
    assert (
        file_path.read_bytes()
        == b"user change"
    )