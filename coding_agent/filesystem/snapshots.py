"""File snapshot model."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from .encoding import decode_text_file
from .versions import (
    FileVersion,
    version_of_file,
)


class FileSnapshot(BaseModel):
    """某一次读取时的文件状态快照。"""

    # workspace-relative path
    path: str

    # 内部统一使用 LF 的文本内容
    content: str

    # 文件版本
    version: FileVersion

    # 当前第一版只支持 UTF-8
    encoding: str = "utf-8"

    # 原始文件换行风格：LF / CRLF
    newline: str = "LF"

    # 原始文件是否存在 UTF-8 BOM
    has_bom: bool = False

    # 快照时间
    captured_at: datetime


def snapshot_file(
    path: Path,
    *,
    workspace_relative: str,
) -> FileSnapshot:
    """读取当前磁盘文件并建立快照。"""

    data = path.read_bytes()

    decoded = decode_text_file(data)

    version = version_of_file(
        path
    )

    return FileSnapshot(
        path=workspace_relative,
        content=decoded.content,
        version=version,
        encoding=decoded.encoding,
        newline=decoded.newline,
        has_bom=decoded.has_bom,
        captured_at=datetime.now(),
    )