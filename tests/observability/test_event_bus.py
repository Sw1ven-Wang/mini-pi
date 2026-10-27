import asyncio

from coding_agent.observability.event_bus import EventBus, InMemoryEventSink
from coding_agent.observability.events import AgentEvent, AgentEventType


def _event(index: int) -> AgentEvent:
    return AgentEvent(
        session_id="session",
        trace_id="trace",
        span_id=f"span-{index}",
        type=AgentEventType.TASK_STARTED,
        payload={"index": index},
    )


class _FailingSink:
    async def emit(self, event):
        raise OSError("log disk unavailable")


async def test_event_bus_emits_to_all_sinks():
    first = InMemoryEventSink()
    second = InMemoryEventSink()
    await EventBus([first, second]).emit(_event(1))
    assert len(first.events) == len(second.events) == 1


async def test_sink_failure_does_not_break_runtime():
    healthy = InMemoryEventSink()
    bus = EventBus([_FailingSink(), healthy])
    await bus.emit(_event(1))
    assert bus.failure_count == 1
    assert len(healthy.events) == 1


async def test_event_order_preserved():
    sink = InMemoryEventSink()
    bus = EventBus([sink])
    await asyncio.gather(*(bus.emit(_event(index)) for index in range(10)))
    assert [event.payload["index"] for event in sink.events] == list(range(10))
