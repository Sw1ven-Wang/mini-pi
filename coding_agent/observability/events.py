"""Safe, structured runtime events for V8."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentEventType(str, Enum):
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    PHASE_CHANGED = "phase.changed"
    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"
    MODEL_REQUEST_STARTED = "model.request.started"
    MODEL_REQUEST_COMPLETED = "model.request.completed"
    MODEL_REQUEST_FAILED = "model.request.failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_APPROVED = "tool.approved"
    TOOL_DENIED = "tool.denied"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    POLICY_EVALUATED = "policy.evaluated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    AUTO_CLASSIFIED = "auto.classified"
    MUTATION_PLANNED = "mutation.planned"
    MUTATION_APPLIED = "mutation.applied"
    MUTATION_ROLLED_BACK = "mutation.rolled_back"
    ROLLBACK_STARTED = "rollback.started"
    ROLLBACK_COMPLETED = "rollback.completed"
    ROLLBACK_FAILED = "rollback.failed"
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_COMPLETED = "verification.completed"
    CONTEXT_COMPACTED = "context.compacted"
    SESSION_SAVED = "session.saved"
    SESSION_RESUMED = "session.resumed"
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_STOPPED = "sandbox.stopped"
    SANDBOX_VIOLATION = "sandbox.violation"


class AgentEvent(BaseModel):
    """An event containing metadata only, never prompts/files/env by default."""

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    session_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    parent_span_id: str | None = None
    type: AgentEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
