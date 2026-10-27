from coding_agent.observability.event_bus import EventBus, InMemoryEventSink
from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.observability.redaction import REDACTED, redact


def test_redact_api_key():
    assert redact({"api_key": "sk-abcdefghijklmnop"}) == {"api_key": REDACTED}


def test_redact_authorization():
    assert redact({"authorization": "Bearer private-value"})["authorization"] == REDACTED


def test_redact_nested_secret():
    result = redact({"safe": {"DEEPSEEK_API_KEY": "TOP_SECRET"}, "text": "Bearer abcdef"})
    assert result["safe"]["DEEPSEEK_API_KEY"] == REDACTED
    assert result["text"] == REDACTED


def test_safe_metadata_unchanged():
    value = {"tool_name": "read", "duration_ms": 12, "paths": ["src/a.py"]}
    assert redact(value) == value


async def test_event_bus_redacts_before_every_sink():
    sink = InMemoryEventSink()
    event = AgentEvent(
        session_id="s",
        trace_id="t",
        span_id="p",
        type=AgentEventType.TASK_STARTED,
        payload={"token": "TOP_SECRET", "safe": "kept"},
    )
    await EventBus([sink]).emit(event)
    assert sink.events[0].payload == {"token": REDACTED, "safe": "kept"}
