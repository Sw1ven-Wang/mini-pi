"""Workspace-level Change IR.

V5 在保留 V3 ChangeSet/FileChange/TextReplacement 的基础上，
扩展出完整文件生命周期操作：Create / Edit / Delete / Move。

所有操作最终都进入同一套 Workspace/Version/Lock/Checkpoint/
Recovery/Permission/Verification 安全体系。
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from coding_agent.changes.ir import ChangeSet, TextReplacement
from coding_agent.filesystem.versions import FileVersion

MAX_OPERATIONS = 50


class CreateFileOp(BaseModel):
    kind: Literal["create_file"] = "create_file"
    path: str
    content: str


class EditFileOp(BaseModel):
    kind: Literal["edit_file"] = "edit_file"
    path: str
    expected_version: FileVersion | None = None
    replacements: list[TextReplacement]


class DeleteFileOp(BaseModel):
    kind: Literal["delete_file"] = "delete_file"
    path: str
    expected_version: FileVersion


class MoveFileOp(BaseModel):
    kind: Literal["move_file"] = "move_file"
    source: str
    destination: str
    expected_source_version: FileVersion


WorkspaceOperation = Annotated[
    Union[
        CreateFileOp,
        EditFileOp,
        DeleteFileOp,
        MoveFileOp,
    ],
    Field(discriminator="kind"),
]


class WorkspaceChangeSet(BaseModel):
    operations: list[WorkspaceOperation]

    @model_validator(mode="after")
    def _check_max_operations(self) -> "WorkspaceChangeSet":
        if len(self.operations) > MAX_OPERATIONS:
            raise ValueError(
                f"operations 数量超过上限 {MAX_OPERATIONS}"
            )
        return self


def text_change_set_to_workspace_change_set(
    change_set: ChangeSet,
) -> WorkspaceChangeSet:
    """把旧 ChangeSet 转为 WorkspaceChangeSet（全部为 EditFileOp）。

    保留 V3 PatchTool 兼容，不一次性破坏旧接口。
    """

    operations: list[WorkspaceOperation] = []

    for file_change in change_set.files:
        operations.append(
            EditFileOp(
                path=file_change.path,
                expected_version=file_change.expected_version,
                replacements=file_change.replacements,
            )
        )

    return WorkspaceChangeSet(operations=operations)
