"""Safety-first deterministic replay into a disposable workspace copy."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.replay.models import TraceRecord
from coding_agent.verification.models import VerificationSummary


ReplayVerifier = Callable[[Workspace], Awaitable[VerificationSummary]]


class ReplayResult(BaseModel):
    trace_id: str
    used_temporary_workspace: bool = True
    source_unchanged: bool
    final_files: dict[str, str] = Field(default_factory=dict)
    verification_summary: dict | None = None


class TraceReplayer:
    """Replays recorded mutations without invoking a model or source checkout."""

    async def replay(
        self,
        record: TraceRecord,
        *,
        source_workspace: Path,
        verifier: ReplayVerifier | None = None,
    ) -> ReplayResult:
        source = source_workspace.resolve(strict=True)
        if not source.is_dir():
            raise ValueError("source_workspace 必须是目录")
        source_before = _file_hashes(source)

        with tempfile.TemporaryDirectory(prefix="mini-pi-replay-") as temp:
            destination = Path(temp) / "workspace"
            shutil.copytree(source, destination, symlinks=True)
            if destination.resolve() == source:
                raise RuntimeError("Replay 拒绝使用源 workspace")
            workspace = Workspace(destination)
            runtime = WorkspaceMutationRuntime(
                workspace, FileMutationQueue(), CheckpointStore()
            )
            for action in record.tool_actions:
                if action.tool_name != "workspace_mutation":
                    raise ValueError(f"unsupported replay tool: {action.tool_name}")
                await runtime.apply(action.change_set)
            verification = await verifier(workspace) if verifier is not None else None
            final_files = _file_hashes(destination)

        source_after = _file_hashes(source)
        if source_after != source_before:
            raise RuntimeError("Replay source workspace changed")
        return ReplayResult(
            trace_id=record.trace_id,
            source_unchanged=True,
            final_files=final_files,
            verification_summary=(
                verification.model_dump(mode="json") if verification is not None else None
            ),
        )


def _file_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result
