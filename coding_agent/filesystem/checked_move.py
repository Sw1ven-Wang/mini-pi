"""Checked move primitive (regular file → regular file path)."""

from __future__ import annotations

import os
from pathlib import Path

from coding_agent.errors import (
    FileAlreadyExistsAgentError,
    FileNotFoundAgentError,
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
    version_of_file,
)


def move_file_checked(
    source: Path,
    destination: Path,
    *,
    expected_source_version: FileVersion,
) -> FileVersion:
    """把 source 移动到 destination，返回移动后的版本。

    前置检查：
        - source 存在且是普通文件
        - source 版本 == expected_source_version
        - destination 不存在
        - source != destination

    Workspace containment 由上层 Workspace.resolve() 负责。
    调用方应同时持有 source 与 destination 的 lock（按 canonical
    resolved path 排序获取）。

    注意：Python 标准库没有跨平台 portable 的 rename-no-replace
    原子原语，因此 destination absence check 与 rename 之间
    仍有 external race window。本 primitive 不声称完全原子。
    """

    if source == destination:
        raise InvalidArgumentError(
            "source 与 destination 不能相同"
        )

    if not source.exists():
        raise FileNotFoundAgentError(str(source))

    if not source.is_file():
        raise InvalidArgumentError(
            f"source 不是普通文件: {source}"
        )

    if destination.exists():
        raise FileAlreadyExistsAgentError(
            str(destination)
        )

    current_version = version_of_file(source)

    if not same_version(
        current_version,
        expected_source_version,
    ):
        raise StaleVersionError(
            expected=expected_source_version.digest,
            current=current_version.digest,
        )

    # 最终 source version recheck 后执行 rename。
    os.rename(source, destination)

    return version_of_file(destination)
