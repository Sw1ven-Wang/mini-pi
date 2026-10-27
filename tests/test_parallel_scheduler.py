import asyncio

import pytest

from ai.messages import ToolCall
from agent.permissions import PermissionDecision, PermissionDecisionType
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from agent.tools import AgentTool, AgentToolResult


class _Allow:
    async def evaluate(self, tool, arguments):
        return PermissionDecision(
            decision=PermissionDecisionType.ALLOW,
            rule_id="allow",
            reason="test",
        )


class _Probe:
    def __init__(self):
        self.active = 0
        self.maximum = 0
        self.started = []


class _Tool(AgentTool):
    description = "probe"
    parameters = {"type": "object"}

    def __init__(self, name, probe, *, mutation=False, gate=None):
        self.name = name
        self.probe = probe
        self.capabilities = frozenset({"write_fs"} if mutation else {"read_fs"})
        self.gate = gate

    async def execute(self, tool_call_id, arguments):
        self.probe.started.append(tool_call_id)
        self.probe.active += 1
        self.probe.maximum = max(self.probe.maximum, self.probe.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            else:
                await asyncio.sleep(0.03)
            return AgentToolResult(content=tool_call_id)
        finally:
            self.probe.active -= 1


def _dispatcher(tools):
    return ToolDispatcher(ToolRegistry(tools), _Allow())


async def test_two_reads_parallel_and_result_order_stable():
    probe = _Probe()
    dispatcher = _dispatcher([_Tool("read_a", probe), _Tool("read_b", probe)])
    results = await dispatcher.dispatch_many([
        ToolCall(id="first", name="read_a", arguments={}),
        ToolCall(id="second", name="read_b", arguments={}),
    ])
    assert probe.maximum == 2
    assert [result.content for result in results] == ["first", "second"]


async def test_read_search_parallel():
    probe = _Probe()
    dispatcher = _dispatcher([_Tool("read", probe), _Tool("search", probe)])
    await dispatcher.dispatch_many([
        ToolCall(id="read", name="read", arguments={}),
        ToolCall(id="search", name="search", arguments={}),
    ])
    assert probe.maximum == 2


async def test_mutations_not_parallel():
    probe = _Probe()
    dispatcher = _dispatcher([
        _Tool("edit", probe, mutation=True),
        _Tool("read", probe),
    ])
    await dispatcher.dispatch_many([
        ToolCall(id="edit", name="edit", arguments={}),
        ToolCall(id="read", name="read", arguments={}),
    ])
    assert probe.maximum == 1
    assert probe.started == ["edit", "read"]


async def test_parallel_cancel_propagates():
    probe = _Probe()
    gate = asyncio.Event()
    dispatcher = _dispatcher([
        _Tool("read_a", probe, gate=gate),
        _Tool("read_b", probe, gate=gate),
    ])
    task = asyncio.create_task(dispatcher.dispatch_many([
        ToolCall(id="a", name="read_a", arguments={}),
        ToolCall(id="b", name="read_b", arguments={}),
    ]))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert probe.active == 0
