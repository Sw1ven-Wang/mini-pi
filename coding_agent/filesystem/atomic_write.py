"""Atomic file write utilities."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
    version_of_file,
)
from coding_agent.testing.fault_injection import NoopFaultInjector


class StaleFileError(RuntimeError):
    """文件在 mutation 执行期间被外部修改。"""


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fault_injector=None,
) -> None:
    """原子写入原始 bytes。

    流程：

    1. 在目标文件同一目录创建临时文件
    2. 写入完整 bytes
    3. flush
    4. fsync
    5. os.replace 原子替换目标文件

    如果临时文件写入过程中失败，
    原目标文件不会处于半写入状态。
    """

    directory = path.parent

    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{path.name}.",
    )

    try:
        with os.fdopen(
            fd,
            "wb",
        ) as f:
            f.write(data)

            f.flush()

            os.fsync(
                f.fileno()
            )

        (fault_injector or NoopFaultInjector()).maybe_fail("before_atomic_replace")
        os.replace(
            temp_path,
            path,
        )

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def atomic_write_bytes_checked(
    path: Path,
    data: bytes,
    *,
    expected_version: FileVersion,
    fault_injector=None,
) -> None:
    """带版本保护的原子写入。

    用于 EditTool 等 read-modify-write mutation。

    流程：

    1. 先完整生成临时文件
    2. flush + fsync
    3. 在 os.replace 前再次读取目标文件版本
    4. current_version == expected_version：
         执行 replace
    5. 版本不同：
         拒绝覆盖并抛出 StaleFileError

    expected_version 应当是本轮 mutation 开始时
    从 snapshot 得到的版本，而不是修改后的版本。
    """

    directory = path.parent

    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix=f".{path.name}.",
    )

    try:
        # ------------------------------------------
        # 1. 先把新文件完整写入临时文件
        # ------------------------------------------

        with os.fdopen(
            fd,
            "wb",
        ) as f:
            f.write(data)

            f.flush()

            os.fsync(
                f.fileno()
            )

        # ------------------------------------------
        # 2. replace 前重新检查真实磁盘状态
        # ------------------------------------------

        if not path.exists():
            raise StaleFileError(
                f"文件在修改期间消失: {path}"
            )

        current_version = version_of_file(
            path
        )

        if not same_version(
            current_version,
            expected_version,
        ):
            raise StaleFileError(
                "文件在修改期间发生变化，"
                "已拒绝覆盖，请重新读取文件"
            )

        # ------------------------------------------
        # 3. 版本仍一致才真正替换
        # ------------------------------------------

        (fault_injector or NoopFaultInjector()).maybe_fail("before_atomic_replace")
        os.replace(
            temp_path,
            path,
        )

    finally:
        # stale 或其他异常发生时，
        # 临时文件也必须清理。
        if os.path.exists(temp_path):
            os.remove(temp_path)


def atomic_write_text(
    path: Path,
    content: str,
    encoding: str = "utf-8",
) -> None:
    """原子写入普通文本。

    convenience wrapper。

    对严格要求 BOM / CRLF 保真的代码修改，
    应使用 encode_text_file() +
    atomic_write_bytes()/atomic_write_bytes_checked()。
    """

    data = content.encode(
        encoding
    )

    atomic_write_bytes(
        path,
        data,
    )
