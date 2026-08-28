"""Tests for OpenAIProvider tool calling (streaming).

覆盖：消息序列化、工具定义下发、单个/多个工具调用的流式聚合、
参数任意分片、纯工具响应，以及非法 JSON 时的错误处理。
"""
import sys
from pathlib import Path

# 获取当前文件的上一级目录（即项目根目录），并加入到模块搜索路径的最前面
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import asyncio
import json
from types import SimpleNamespace

from ai.context import Context
from ai.events import StreamEnd, StreamError, StreamStart, ToolCallReady
from ai.messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from ai.models import Model
from ai.providers.openai import OpenAIProvider
from ai.tools import ToolDefinition


# ---------- 伪造 OpenAI 客户端的辅助类（不联网） ----------

def _chunk(*, content=None, tool_calls=None):
    """构造一个类似 SDK ChatCompletionChunk 的对象。"""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, id=None, name=None, arguments=None):
    """构造一个流式工具调用的分片 delta。"""
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=function)


class _FakeClient:
    """伪造 AsyncOpenAI：chat.completions.create 返回异步迭代器，并记录每次调用。"""

    def __init__(self, chunks):
        self._chunks = chunks
        self.chat = self
        self.completions = self
        self.calls = []  # 记录每次 create 的完整请求参数

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _AsyncChunks(self._chunks)


class _AsyncChunks:
    def __init__(self, chunks):
        self._it = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


# ---------- 公共辅助 ----------

def _weather_tool():
    return ToolDefinition(
        name="get_weather",
        description="查询城市天气",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )


def _model():
    return Model(id="deepseek-v4-flash", provider="deepseek")


def _context():
    return Context(messages=[UserMessage(content="查询天气")])


def _collect_events(provider, model, context, tools):
    """驱动 stream() 跑完并收集所有事件。"""

    async def run():
        return [event async for event in provider.stream(model, context, tools)]

    return asyncio.run(run())


# ---------- serialization ----------

def test_assistant_message_with_tool_calls_to_openai():
    provider = OpenAIProvider(client=_FakeClient([]))
    msg = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(id="call_1", name="get_weather", arguments={"city": "Hangzhou"})
        ],
    )
    out = provider._message_to_openai(msg)

    assert out["role"] == "assistant"
    # 带 tool_calls 时 content 必须是 None（序列化为 JSON null），
    # 不能是空字符串 ""——OpenAI 对 "" + tool_calls 会返回 400。
    assert out["content"] is None
    assert out["tool_calls"][0]["id"] == "call_1"
    assert out["tool_calls"][0]["type"] == "function"
    assert out["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == {
        "city": "Hangzhou"
    }


def test_tool_result_message_to_openai():
    provider = OpenAIProvider(client=_FakeClient([]))
    msg = ToolResultMessage(
        tool_call_id="call_1", name="get_weather", content="sunny"
    )
    out = provider._message_to_openai(msg)
    assert out == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}


# ---------- tool definition ----------

def test_stream_sends_tools_to_openai():
    client = _FakeClient([])
    provider = OpenAIProvider(client=client)
    _collect_events(provider, _model(), _context(), [_weather_tool()])

    assert len(client.calls) == 1
    assert client.calls[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]


# ---------- single tool streaming ----------

def test_stream_emits_single_tool_call_ready():
    chunks = [
        _chunk(tool_calls=[_tool_delta(0, id="call_1", name="get_weather")]),
        _chunk(tool_calls=[_tool_delta(0, arguments='{"city":')]),
        _chunk(tool_calls=[_tool_delta(0, arguments='"Hangzhou"}')]),
    ]
    provider = OpenAIProvider(client=_FakeClient(chunks))
    events = _collect_events(provider, _model(), _context(), [_weather_tool()])

    assert isinstance(events[0], StreamStart)
    assert isinstance(events[-1], StreamEnd)

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].tool_call.id == "call_1"
    assert ready[0].tool_call.name == "get_weather"
    assert ready[0].tool_call.arguments == {"city": "Hangzhou"}

    assert events[-1].message.tool_calls[0].arguments == {"city": "Hangzhou"}


def test_tool_arguments_can_be_split_arbitrarily():
    # 参数被拆成极小分片，逼实现用字符串累积而非逐块 json.loads
    fragments = ["{", '"ci', 'ty"', ":", '"Hang', 'zhou"', "}"]
    chunks = [
        _chunk(tool_calls=[_tool_delta(0, id="call_1", name="get_weather")]),
    ] + [_chunk(tool_calls=[_tool_delta(0, arguments=frag)]) for frag in fragments]

    provider = OpenAIProvider(client=_FakeClient(chunks))
    events = _collect_events(provider, _model(), _context(), [_weather_tool()])

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].tool_call.arguments == {"city": "Hangzhou"}


# ---------- parallel / multiple tool streaming ----------

def test_stream_aggregates_multiple_tool_calls_by_index():
    # 两个工具调用真正交错：0 和 1 的参数分片互相穿插到达
    chunks = [
        _chunk(tool_calls=[_tool_delta(0, id="call_1", name="get_weather")]),
        _chunk(tool_calls=[_tool_delta(1, id="call_2", name="get_weather")]),
        _chunk(tool_calls=[_tool_delta(0, arguments='{"city":')]),     # 0 part1
        _chunk(tool_calls=[_tool_delta(1, arguments='{"city":')]),     # 1 part1
        _chunk(tool_calls=[_tool_delta(1, arguments='"Shanghai"}')]),  # 1 part2
        _chunk(tool_calls=[_tool_delta(0, arguments='"Hangzhou"}')]),  # 0 part2
    ]
    provider = OpenAIProvider(client=_FakeClient(chunks))
    events = _collect_events(provider, _model(), _context(), [_weather_tool()])

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 2

    calls = {e.tool_call.id: e.tool_call for e in ready}
    assert calls["call_1"].name == "get_weather"
    assert calls["call_1"].arguments == {"city": "Hangzhou"}
    assert calls["call_2"].name == "get_weather"
    assert calls["call_2"].arguments == {"city": "Shanghai"}

    end = events[-1]
    assert isinstance(end, StreamEnd)
    final_calls = {call.id: call for call in end.message.tool_calls}
    assert final_calls["call_1"].arguments == {"city": "Hangzhou"}
    assert final_calls["call_2"].arguments == {"city": "Shanghai"}


# ---------- tool-only ----------

def test_tool_only_response_has_empty_content():
    chunks = [
        _chunk(tool_calls=[_tool_delta(0, id="call_1", name="get_weather")]),
        _chunk(tool_calls=[_tool_delta(0, arguments='{"city":"Hangzhou"}')]),
    ]
    provider = OpenAIProvider(client=_FakeClient(chunks))
    events = _collect_events(provider, _model(), _context(), [_weather_tool()])

    end = events[-1]
    assert isinstance(end, StreamEnd)
    assert end.message.content == ""
    assert len(end.message.tool_calls) == 1


# ---------- invalid data ----------

def test_invalid_tool_arguments_emits_stream_error():
    # 参数 JSON 不完整（流直接结束）→ 必须 StreamError，绝不能产出 ToolCallReady
    chunks = [
        _chunk(tool_calls=[_tool_delta(0, id="call_1", name="get_weather")]),
        _chunk(tool_calls=[_tool_delta(0, arguments='{"city":')]),
    ]
    provider = OpenAIProvider(client=_FakeClient(chunks))
    events = _collect_events(provider, _model(), _context(), [_weather_tool()])

    assert isinstance(events[0], StreamStart)

    errors = [e for e in events if isinstance(e, StreamError)]
    assert len(errors) == 1
    assert isinstance(events[-1], StreamError)

    # 关键安全断言：绝不能产出 ToolCallReady（把损坏伪装成空参数工具调用）
    assert not any(isinstance(e, ToolCallReady) for e in events)
