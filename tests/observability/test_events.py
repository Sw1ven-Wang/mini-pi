from coding_agent.observability.events import AgentEvent, AgentEventType


def _event() -> AgentEvent:
    return AgentEvent(
        session_id="session-1",
        trace_id="trace-1",
        span_id="span-1",
        type=AgentEventType.TASK_STARTED,
    )


def test_event_has_trace_id():
    assert _event().trace_id == "trace-1"


def test_event_has_span_id():
    assert _event().span_id == "span-1"


def test_event_type_enum():
    assert AgentEventType("mutation.applied") is AgentEventType.MUTATION_APPLIED


def test_event_payload_default():
    first = _event()
    second = _event()
    assert first.payload == {}
    assert first.payload is not second.payload
