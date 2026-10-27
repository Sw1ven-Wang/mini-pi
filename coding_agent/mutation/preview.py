"""Read-only, bounded mutation previews for interactive approval UIs."""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from coding_agent.changes.diff import unified_diff
from coding_agent.mutation.models import (
    PlannedCreate,
    PlannedDelete,
    PlannedEdit,
    PlannedMove,
    PlannedWorkspaceChangeSet,
)


MAX_PREVIEW_CHARS = 500_000
MAX_OPERATION_PREVIEW_CHARS = 200_000


class MutationPreviewOperation(BaseModel):
    kind: str
    path: str | None = None
    source: str | None = None
    destination: str | None = None
    diff: str = ""
    truncated: bool = False


class MutationPreview(BaseModel):
    preview_id: str = Field(default_factory=lambda: str(uuid4()))
    operations: list[MutationPreviewOperation] = Field(default_factory=list)
    truncated: bool = False


class MutationPreviewObserver(Protocol):
    async def publish(self, preview: MutationPreview) -> None: ...


def build_mutation_preview(
    planned: PlannedWorkspaceChangeSet,
) -> MutationPreview:
    """Convert a validated, read-only plan into a bounded UI representation."""

    operations: list[MutationPreviewOperation] = []
    remaining = MAX_PREVIEW_CHARS
    preview_truncated = False

    for operation in planned.operations:
        item = _operation_preview(operation)
        allowed = min(MAX_OPERATION_PREVIEW_CHARS, max(remaining, 0))
        if len(item.diff) > allowed:
            item.diff = item.diff[:allowed]
            item.truncated = True
            preview_truncated = True
        remaining -= len(item.diff)
        operations.append(item)

    return MutationPreview(
        operations=operations,
        truncated=preview_truncated,
    )


def _operation_preview(operation) -> MutationPreviewOperation:
    if isinstance(operation, PlannedCreate):
        return MutationPreviewOperation(
            kind="create_file",
            path=operation.path,
            diff=unified_diff("", operation.content, operation.path),
        )
    if isinstance(operation, PlannedEdit):
        return MutationPreviewOperation(
            kind="edit_file",
            path=operation.path,
            diff=operation.diff,
        )
    if isinstance(operation, PlannedDelete):
        return MutationPreviewOperation(
            kind="delete_file",
            path=operation.path,
            diff=unified_diff(operation.snapshot.content, "", operation.path),
        )
    if isinstance(operation, PlannedMove):
        return MutationPreviewOperation(
            kind="move_file",
            source=operation.source,
            destination=operation.destination,
        )
    raise AssertionError(f"unsupported planned operation: {type(operation)!r}")
