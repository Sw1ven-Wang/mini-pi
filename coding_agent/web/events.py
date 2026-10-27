"""Bounded SSE event buffering with reconnect support."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable

from coding_agent.observability.events import AgentEvent
from coding_agent.observability.redaction import redact_event


EventCallback = Callable[[AgentEvent], Awaitable[None]]


class WebEventSink:
    def __init__(
        self,
        *,
        max_events: int = 500,
        subscriber_queue_size: int = 100,
        on_event: EventCallback | None = None,
        runtime_execution=None,
    ) -> None:
        if max_events < 1 or subscriber_queue_size < 1:
            raise ValueError("event buffer limits must be positive")
        self._events: deque[tuple[int, AgentEvent]] = deque(maxlen=max_events)
        self._subscriber_queue_size = subscriber_queue_size
        self._subscribers: set[asyncio.Queue] = set()
        self._next_sequence = 1
        self._lock = asyncio.Lock()
        self._closed = False
        self._on_event = on_event
        self.runtime_execution = runtime_execution

    @property
    def closed(self) -> bool:
        return self._closed

    async def emit(self, event: AgentEvent) -> None:
        safe_event = redact_event(event).model_copy(deep=True)
        async with self._lock:
            if self._closed:
                return
            sequence = self._next_sequence
            if self.runtime_execution is not None:
                execution = self.runtime_execution
                recent = execution.store.events(execution.run_id, max(0, execution.run.event_sequence - 1))
                previous = recent[-1] if recent else {}
                persisted = previous.get("payload", {}).get("agent_event", {})
                if persisted.get("event_id") != safe_event.event_id:
                    execution.update(event="agent_event", payload={"agent_event": safe_event.model_dump(mode="json")})
                sequence = execution.run.event_sequence
            self._next_sequence += 1
            self._events.append((sequence, safe_event))
            subscribers = list(self._subscribers)
            for queue in subscribers:
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover
                        pass
                queue.put_nowait((sequence, safe_event.model_copy(deep=True)))
        if self._on_event is not None:
            await self._on_event(safe_event)

    async def stream(
        self,
        after_sequence: int = 0,
        *,
        heartbeat_seconds: float | None = None,
    ) -> AsyncIterator[tuple[int | None, AgentEvent | None]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._lock:
            backlog = [item for item in self._events if item[0] > after_sequence]
            if self.runtime_execution is not None:
                backlog = self._persisted_events(after_sequence)
            closed = self._closed
            if not closed:
                self._subscribers.add(queue)
        try:
            for item in backlog:
                yield item
            if closed:
                return
            while True:
                try:
                    if heartbeat_seconds is None:
                        item = await queue.get()
                    else:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=heartbeat_seconds,
                        )
                except asyncio.TimeoutError:
                    yield None, None
                    continue
                if item is None:
                    return
                yield item
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            subscribers = list(self._subscribers)
            for queue in subscribers:
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover
                        pass
                queue.put_nowait(None)

    async def snapshot(self) -> list[tuple[int, AgentEvent]]:
        async with self._lock:
            if self.runtime_execution is not None:
                return self._persisted_events(0)
            return [
                (sequence, event.model_copy(deep=True))
                for sequence, event in self._events
            ]

    def _persisted_events(self, after):
        execution = self.runtime_execution
        # Bounded replay: old cursors resynchronize through the task snapshot.
        watermark = execution.run.event_sequence
        start = max(after, watermark - self._events.maxlen)
        result = []
        if after > 0 and after < start:
            from coding_agent.observability.events import AgentEventType
            result.append((start, AgentEvent(session_id=execution.run.session_id,
                trace_id="runtime", span_id="replay", type=AgentEventType.PHASE_CHANGED,
                payload={"snapshot_required": True})))
        for item in execution.store.events(execution.run_id, start, self._events.maxlen):
            value = item.get("payload", {}).get("agent_event")
            if value:
                result.append((item["sequence"], AgentEvent.model_validate(value)))
        return result
