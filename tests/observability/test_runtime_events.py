from pathlib import Path

from ai.messages import ToolCall
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from agent.tools import AgentTool, AgentToolResult
from coding_agent.observability.event_bus import EventBus, InMemoryEventSink
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.runtime import ToolEventObserver
from coding_agent.observability.tracing import TraceContext
from coding_agent.safety.permissions import CodingPermissionEngine


class _ReadTool(AgentTool):
    name = "read"
    description = "read"
    capabilities = frozenset({"read_fs"})
    parameters = {"type": "object"}

    async def execute(self, tool_call_id, arguments):
        return AgentToolResult(content="ok")


class _DeleteTool(_ReadTool):
    name = "delete"
    capabilities = frozenset({"delete_file"})


def _dispatcher(tool, sink):
    trace = TraceContext.root()
    observer = ToolEventObserver(
        event_bus=EventBus([sink]), session_id="session", trace=trace
    )
    return ToolDispatcher(
        ToolRegistry([tool]),
        CodingPermissionEngine(),
        lifecycle_observer=observer,
    )


async def test_tool_lifecycle_records_safe_order_without_arguments():
    sink = InMemoryEventSink()
    result = await _dispatcher(_ReadTool(), sink).dispatch(
        ToolCall(id="call", name="read", arguments={"api_key": "TOP_SECRET"})
    )

    assert result.is_error is False
    assert [event.type for event in sink.events] == [
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.POLICY_EVALUATED,
        AgentEventType.TOOL_APPROVED,
        AgentEventType.TOOL_STARTED,
        AgentEventType.TOOL_COMPLETED,
    ]
    assert "TOP_SECRET" not in str([event.payload for event in sink.events])
    assert all(event.trace_id == sink.events[0].trace_id for event in sink.events)
    assert len({event.span_id for event in sink.events}) == 1


async def test_permission_denial_is_a_terminal_tool_event():
    sink = InMemoryEventSink()
    result = await _dispatcher(_DeleteTool(), sink).dispatch(
        ToolCall(id="call", name="delete", arguments={"path": "file.py"})
    )

    assert result.is_error is True
    assert [event.type for event in sink.events] == [
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.POLICY_EVALUATED,
        AgentEventType.APPROVAL_REQUESTED,
        AgentEventType.TOOL_DENIED,
    ]
