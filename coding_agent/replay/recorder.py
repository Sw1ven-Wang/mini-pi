"""Redacted in-memory trace recorder with atomic persistence."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.observability.artifacts import ArtifactRef
from coding_agent.observability.events import AgentEvent
from coding_agent.observability.metrics import MetricsSink, RunMetrics
from coding_agent.observability.redaction import redact, redact_event
from coding_agent.replay.models import RecordedToolAction, TraceRecord


class TraceRecorder:
    """Event sink that deliberately excludes prompts, env and file bodies."""

    def __init__(self, metrics_sink: MetricsSink | None = None) -> None:
        self.events: list[AgentEvent] = []
        self.tool_actions: list[RecordedToolAction] = []
        self.artifacts: list[ArtifactRef] = []
        self.verification_summary: dict[str, Any] | None = None
        self.metrics_sink = metrics_sink

    async def emit(self, event: AgentEvent) -> None:
        safe = redact_event(event)
        self.events.append(safe)
        if self.metrics_sink is not None:
            await self.metrics_sink.emit(safe)

    def record_action(
        self,
        change_set: WorkspaceChangeSet,
        *,
        safe_arguments: dict[str, Any] | None = None,
        mutation_id: str | None = None,
    ) -> RecordedToolAction:
        safe_change_set = WorkspaceChangeSet.model_validate(
            redact(change_set.model_dump(mode="json"))
        )
        action = RecordedToolAction(
            safe_arguments=redact(safe_arguments or {}),
            change_set=safe_change_set,
            mutation_id=mutation_id,
        )
        self.tool_actions.append(action)
        return action

    def add_artifact(self, reference: ArtifactRef) -> None:
        self.artifacts.append(reference)

    def set_verification_summary(self, summary: dict[str, Any]) -> None:
        self.verification_summary = redact(summary)

    def finalize(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        metrics: RunMetrics | None = None,
    ) -> TraceRecord:
        if not self.events and (trace_id is None or session_id is None):
            raise ValueError("empty trace 需要显式 trace_id 与 session_id")
        resolved_trace = trace_id or self.events[0].trace_id
        resolved_session = session_id or self.events[0].session_id
        matching = [event for event in self.events if event.trace_id == resolved_trace]
        if metrics is None and self.metrics_sink is not None:
            metrics = self.metrics_sink.snapshot(resolved_trace)
        return TraceRecord(
            trace_id=resolved_trace,
            session_id=resolved_session,
            started_at=(matching[0].timestamp if matching else datetime.now(timezone.utc)),
            completed_at=(matching[-1].timestamp if matching else None),
            events=matching,
            metrics=metrics or RunMetrics(),
            artifacts=list(self.artifacts),
            tool_actions=list(self.tool_actions),
            verification_summary=self.verification_summary,
        )

    @staticmethod
    def save(record: TraceRecord, destination: Path) -> None:
        destination = destination.resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            fd, temporary = tempfile.mkstemp(
                dir=destination.parent, prefix=".trace.", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.remove(temporary)
