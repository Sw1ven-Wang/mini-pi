"""Per-file mutation queue."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator


class FileMutationQueue:
    """为每个真实文件维护独立的 asyncio.Lock。

    同一个文件：
        mutation 串行执行。

    不同文件：
        mutation 可以并行执行。
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _key_for(
        self,
        path: Path,
    ) -> str:
        """生成文件锁使用的 canonical key。

        resolve() 很重要：

        real.txt
        alias.txt -> real.txt

        如果 alias 是 symlink，
        两者应该落到同一个锁。
        """

        return str(
            path.resolve(strict=False)
        )

    def _lock_for(
        self,
        path: Path,
    ) -> asyncio.Lock:
        """获取某个文件对应的 lock。"""

        key = self._key_for(path)

        lock = self._locks.get(key)

        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock

        return lock

    @asynccontextmanager
    async def hold(
        self,
        path: Path,
    ) -> AsyncIterator[None]:
        """锁住一个文件的完整 mutation transaction."""

        lock = self._lock_for(path)

        await lock.acquire()

        try:
            yield

        finally:
            lock.release()