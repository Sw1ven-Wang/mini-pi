"""Portable, redacted records used by V8 replay."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.observability.artifacts import ArtifactRef
from coding_agent.observability.events import AgentEvent
from coding_agent.observability.metrics import RunMetrics


class RecordedToolAction(BaseModel):
    """A deterministic mutation invocation, not a model response."""

    action_id: str = Field(default_factory=lambda: str(uuid4()))
    tool_name: str = "workspace_mutation"
    safe_arguments: dict[str, Any] = Field(default_factory=dict)
    change_set: WorkspaceChangeSet
    mutation_id: str | None = None


class TraceRecord(BaseModel):
    trace_id: str
    session_id: str
    started_at: datetime
    completed_at: datetime | None = None
    events: list[AgentEvent] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    tool_actions: list[RecordedToolAction] = Field(default_factory=list)
    verification_summary: dict[str, Any] | None = None
