"""Trace/span identity and event construction."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from coding_agent.observability.events import AgentEvent, AgentEventType


class TraceContext(BaseModel):
    trace_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    parent_span_id: str | None = None

    @classmethod
    def root(cls) -> "TraceContext":
        return cls(trace_id=str(uuid4()), span_id=str(uuid4()))

    def child(self) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=str(uuid4()),
            parent_span_id=self.span_id,
        )

    def event(
        self,
        *,
        session_id: str,
        type: AgentEventType,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        return AgentEvent(
            session_id=session_id,
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            type=type,
            payload=payload or {},
        )
