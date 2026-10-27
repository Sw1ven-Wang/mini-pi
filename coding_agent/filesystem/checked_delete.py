"""Checked delete primitive + expected file state model."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, model_validator

from coding_agent.errors import (
    FileNotFoundAgentError,
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
    version_of_file,
)


class ExpectedFileState(BaseModel):
    """描述 rollback 前磁盘应当处于的文件状态。

    约束：
        exists=True   → version required
        exists=False  → version must be None
    """

    exists: bool
    version: FileVersion | None = None

    @model_validator(mode="after")
    def _check_version_consistency(self) -> "ExpectedFileState":
        if self.exists and self.version is None:
            raise ValueError(
                "exists=True 时 version 不能为 None"
            )

        if not self.exists and self.version is not None:
            raise ValueError(
                "exists=False 时 version 必须为 None"
            )

        return self


def delete_file_checked(
    path: Path,
    *,
    expected_version: FileVersion,
) -> FileVersion:
    """删除一个普通文件，返回删除前的版本。

    前置检查：
        - 文件存在
        - 是普通文件（拒绝目录）
        - 当前版本 == expected_version

    Workspace containment 由上层 Workspace.resolve() 负责。

    注意：version check 与 unlink 之间存在极小 external race
    window，本 primitive 不声称是跨进程事务级 compare-and-delete。
    调用方应把它置于 FileMutationQueue 内，并在 unlink 前最后一次
    recheck。
    """

    if not path.exists():
        raise FileNotFoundAgentError(str(path))

    if not path.is_file():
        raise InvalidArgumentError(
            f"目标不是普通文件: {path}"
        )

    current_version = version_of_file(path)

    if not same_version(current_version, expected_version):
        raise StaleVersionError(
            expected=expected_version.digest,
            current=current_version.digest,
        )

    path.unlink()

    return current_version
