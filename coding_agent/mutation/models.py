"""Planned operation models and recovery reporting models."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from coding_agent.changes.models import ChangedRange, MutationResult
from coding_agent.filesystem.snapshots import FileSnapshot
from coding_agent.filesystem.versions import FileVersion


class PlannedCreate(BaseModel):
    kind: Literal["create_file"] = "create_file"
    path: str
    content: str


class PlannedEdit(BaseModel):
    kind: Literal["edit_file"] = "edit_file"
    path: str
    snapshot: FileSnapshot
    new_content: str
    diff: str
    changed_ranges: list[ChangedRange]


class PlannedDelete(BaseModel):
    kind: Literal["delete_file"] = "delete_file"
    path: str
    snapshot: FileSnapshot


class PlannedMove(BaseModel):
    kind: Literal["move_file"] = "move_file"
    source: str
    destination: str
    source_snapshot: FileSnapshot


PlannedOperation = Annotated[
    Union[
        PlannedCreate,
        PlannedEdit,
        PlannedDelete,
        PlannedMove,
    ],
    Field(discriminator="kind"),
]


class PlannedWorkspaceChangeSet(BaseModel):
    operations: list[PlannedOperation]
    lock_paths: list[str]


class MoveRecoveryRecord(BaseModel):
    """Move 的 recovery 信息：记录 source/destination 的 checkpoint。"""

    source: str
    destination: str
    source_checkpoint_id: str
    destination_checkpoint_id: str
    moved_version: FileVersion


class RecoveryFailure(BaseModel):
    operation_index: int
    paths: list[str]
    error_code: str
    message: str


class RecoveryReport(BaseModel):
    attempted: int
    recovered: int
    failures: list[RecoveryFailure] = Field(
        default_factory=list
    )

    @property
    def complete(self) -> bool:
        return (
            self.recovered == self.attempted
            and not self.failures
        )


class WorkspaceMutationResult(BaseModel):
    """一次 WorkspaceChangeSet 应用的完整结果。"""

    mutations: list[MutationResult]
    changed_files: list[str] = Field(default_factory=list)
