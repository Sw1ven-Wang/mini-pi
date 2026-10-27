"""Best-effort ordered event fan-out."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from coding_agent.observability.events import AgentEvent
from coding_agent.observability.redaction import redact_event


logger = logging.getLogger(__name__)


class EventSink(Protocol):
    async def emit(self, event: AgentEvent) -> None: ...


class EventBus:
    """Emit in order; observability failures never become domain failures."""

    def __init__(self, sinks: list[EventSink] | None = None) -> None:
        self.sinks = list(sinks or [])
        self.failure_count = 0
        self._lock = asyncio.Lock()

    async def emit(self, event: AgentEvent) -> None:
        safe_event = redact_event(event)
        async with self._lock:
            for sink in self.sinks:
                try:
                    await sink.emit(safe_event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failure_count += 1
                    logger.exception(
                        "observability sink failed (event=%s, sink=%s)",
                        safe_event.type.value,
                        type(sink).__name__,
                    )


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def emit(self, event: AgentEvent) -> None:
        self.events.append(event.model_copy(deep=True))
