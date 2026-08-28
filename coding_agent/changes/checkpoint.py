"""Checkpoint storage."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from coding_agent.filesystem.versions import (
    FileVersion,
    version_of_file,
)


class FileCheckpoint(BaseModel):
    """某个文件 mutation 前的完整状态。"""

    id: str

    path: str

    existed: bool

    content: bytes | None

    version: FileVersion | None

    created_at: datetime


class CheckpointStore:
    """第一版内存 Checkpoint Store."""

    def __init__(self) -> None:
        self._checkpoints: dict[
            str,
            FileCheckpoint,
        ] = {}

    def create(
        self,
        *,
        path: Path,
        workspace_relative: str,
    ) -> FileCheckpoint:
        """捕获目标文件当前的完整 bytes 状态。"""

        checkpoint_id = str(
            uuid.uuid4()
        )

        if path.exists():

            content = path.read_bytes()

            version = version_of_file(
                path
            )

            checkpoint = FileCheckpoint(
                id=checkpoint_id,
                path=workspace_relative,
                existed=True,
                content=content,
                version=version,
                created_at=datetime.now(),
            )

        else:

            checkpoint = FileCheckpoint(
                id=checkpoint_id,
                path=workspace_relative,
                existed=False,
                content=None,
                version=None,
                created_at=datetime.now(),
            )

        self._checkpoints[
            checkpoint_id
        ] = checkpoint

        return checkpoint

    def get(
        self,
        checkpoint_id: str,
    ) -> FileCheckpoint | None:
        return self._checkpoints.get(
            checkpoint_id
        )