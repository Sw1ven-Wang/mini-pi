"""File version utilities."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel


class FileVersion(BaseModel):
    """文件版本信息."""

    algorithm: str = "sha256"

    digest: str

    size_bytes: int

    mtime_ns: int | None = None



def version_from_bytes(
    data: bytes,
    *,
    path: Path | None = None,
) -> FileVersion:
    """根据原始 bytes 生成版本."""

    digest = hashlib.sha256(
        data
    ).hexdigest()


    mtime_ns = None

    if path is not None and path.exists():
        mtime_ns = (
            path.stat()
            .st_mtime_ns
        )


    return FileVersion(
        algorithm="sha256",
        digest=digest,
        size_bytes=len(data),
        mtime_ns=mtime_ns,
    )



def version_of_file(
    path: Path,
) -> FileVersion:
    """计算文件当前版本."""

    data = path.read_bytes()

    return version_from_bytes(
        data,
        path=path,
    )

def same_version(
    current: FileVersion,
    expected: FileVersion,
) -> bool:

    return (
        current.algorithm == expected.algorithm
        and
        current.digest == expected.digest
        and
        current.size_bytes == expected.size_bytes
    )