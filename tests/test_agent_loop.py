"""Tests for the minimal agent loop.

覆盖：

1. 无工具调用时，一轮直接返回最终 AssistantMessage。
2. 有工具调用时：
   LLM -> ToolCall -> ToolDispatcher -> ToolResultMessage -> LLM。
3. Context 消息顺序正确。
4. Provider 调用次数正确。
5. Tool 执行错误能够作为 ToolResultMessage 回灌给下一轮模型。
6. max_turns 能阻止无限工具循环。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ai.models import Model
from ai.tools import ToolDefinition

from agent.agent_loop import (
    MaxTurnsExceededError,
    run_agent_loop,
)
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry

from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.read import ReadTool


# ---------------------------------------------------------------------------
# Fake Provider
# ---------------------------------------------------------------------------


class FakeProvider:
    """一个完全不联网的 Provider。

    每调用一次 stream()，就取出 responses 中的下一个 AssistantMessage，
    包装成：

        StreamStart
        StreamEnd(message=...)

    用来精确控制每一轮 LLM 返回。
    """

    def __init__(self, responses: list[AssistantMessage]) -> None:
        self._responses = list(responses)

        # 记录每一次 Provider 调用时看到的输入。
        self.calls: list[dict] = []

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        """模拟 Provider.stream()。"""

        self.calls.append(
            {
                "model": model,
                "messages": list(context.messages),
                "tools": list(tools),
            }
        )

        yield StreamStart()

        if not self._responses:
            raise RuntimeError("FakeProvider 没有更多预设 response")

        response = self._responses.pop(0)

        yield StreamEnd(message=response)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(id="fake-model", provider="fake")


def _dispatcher(tmp_path: Path) -> ToolDispatcher:
    read_tool = ReadTool(Workspace(tmp_path))

    return ToolDispatcher(
        registry=ToolRegistry([read_tool]),
        permission_engine=CodingPermissionEngine(),
    )


# ---------------------------------------------------------------------------
# 1. 没有工具调用
# ---------------------------------------------------------------------------


def test_agent_loop_returns_immediately_without_tool_calls(tmp_path: Path):
    """模型第一轮没有 ToolCall 时，应直接结束。"""

    final_message = AssistantMessage(
        content="hello",
        tool_calls=[],
    )

    provider = FakeProvider(responses=[final_message])

    context = Context(messages=[UserMessage(content="你好")])

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_dispatcher=_dispatcher(tmp_path),
        )
    )

    assert result.content == "hello"
    assert result.tool_calls == []

    assert len(provider.calls) == 1
    assert len(context.messages) == 2
    assert isinstance(context.messages[0], UserMessage)
    assert isinstance(context.messages[1], AssistantMessage)
    assert context.messages[1].content == "hello"


# ---------------------------------------------------------------------------
# 2. ToolCall -> ToolResult -> 第二轮 LLM
# ---------------------------------------------------------------------------


def test_agent_loop_executes_tool_and_calls_model_again(tmp_path: Path):
    """完整验证 User -> Assistant(tool_call) -> ReadTool -> ToolResult -> LLM。"""

    file_path = tmp_path / "hello.txt"
    file_path.write_text("hello mini pi", encoding="utf-8")

    first_response = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                name="read",
                arguments={"path": "hello.txt"},
            )
        ],
    )

    second_response = AssistantMessage(
        content="文件内容是 hello mini pi",
        tool_calls=[],
    )

    provider = FakeProvider(responses=[first_response, second_response])

    context = Context(messages=[UserMessage(content="请读取 hello.txt")])

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_dispatcher=_dispatcher(tmp_path),
        )
    )

    assert result.content == "文件内容是 hello mini pi"
    assert result.tool_calls == []

    assert len(provider.calls) == 2

    assert len(context.messages) == 4
    assert isinstance(context.messages[0], UserMessage)
    assert isinstance(context.messages[1], AssistantMessage)
    assert isinstance(context.messages[2], ToolResultMessage)
    assert isinstance(context.messages[3], AssistantMessage)

    assert context.messages[1].tool_calls[0].name == "read"

    tool_result = context.messages[2]
    assert tool_result.tool_call_id == "call_1"
    assert tool_result.name == "read"
    assert tool_result.content == "hello mini pi"
    assert tool_result.is_error is False

    assert context.messages[3].content == "文件内容是 hello mini pi"

    second_call_messages = provider.calls[1]["messages"]
    assert len(second_call_messages) == 3
    assert isinstance(second_call_messages[0], UserMessage)
    assert isinstance(second_call_messages[1], AssistantMessage)
    assert isinstance(second_call_messages[2], ToolResultMessage)
    assert second_call_messages[2].content == "hello mini pi"

    first_call_tools = provider.calls[0]["tools"]
    assert len(first_call_tools) == 1
    assert first_call_tools[0].name == "read"


# ---------------------------------------------------------------------------
# 3. 工具异常也应该回灌模型
# ---------------------------------------------------------------------------


def test_agent_loop_feeds_tool_error_back_to_model(tmp_path: Path):
    """ReadTool 文件不存在时，错误也应回灌模型。"""

    first_response = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id="call_missing",
                name="read",
                arguments={"path": "missing.txt"},
            )
        ],
    )

    second_response = AssistantMessage(
        content="文件不存在。",
        tool_calls=[],
    )

    provider = FakeProvider(responses=[first_response, second_response])

    context = Context(messages=[UserMessage(content="读取 missing.txt")])

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_dispatcher=_dispatcher(tmp_path),
        )
    )

    assert result.content == "文件不存在。"
    assert len(provider.calls) == 2
    assert len(context.messages) == 4

    tool_result = context.messages[2]
    assert isinstance(tool_result, ToolResultMessage)
    assert tool_result.tool_call_id == "call_missing"
    assert tool_result.name == "read"
    assert tool_result.is_error is True
    assert tool_result.content != ""

    second_call_messages = provider.calls[1]["messages"]
    error_message = second_call_messages[-1]
    assert isinstance(error_message, ToolResultMessage)
    assert error_message.is_error is True


# ---------------------------------------------------------------------------
# 4. 未知工具也应该回灌模型
# ---------------------------------------------------------------------------


def test_agent_loop_feeds_unknown_tool_error_back_to_model(tmp_path: Path):
    """模型调用不存在的工具时，Agent 不应崩溃。"""

    first_response = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id="call_unknown",
                name="does_not_exist",
                arguments={},
            )
        ],
    )

    second_response = AssistantMessage(
        content="这个工具不可用。",
        tool_calls=[],
    )

    provider = FakeProvider(responses=[first_response, second_response])

    context = Context(messages=[UserMessage(content="执行一个不存在的工具")])

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_dispatcher=_dispatcher(tmp_path),
        )
    )

    assert result.content == "这个工具不可用。"

    tool_result = context.messages[2]
    assert isinstance(tool_result, ToolResultMessage)
    assert tool_result.tool_call_id == "call_unknown"
    assert tool_result.name == "does_not_exist"
    assert tool_result.is_error is True


# ---------------------------------------------------------------------------
# 5. max_turns
# ---------------------------------------------------------------------------


def test_agent_loop_stops_after_max_turns(tmp_path: Path):
    """如果模型一直调用工具，必须由 max_turns 强制停止。"""

    file_path = tmp_path / "hello.txt"
    file_path.write_text("hello", encoding="utf-8")

    responses = [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"call_{i}",
                    name="read",
                    arguments={"path": "hello.txt"},
                )
            ],
        )
        for i in range(3)
    ]

    provider = FakeProvider(responses)

    context = Context(messages=[UserMessage(content="一直读文件")])

    with pytest.raises(MaxTurnsExceededError):
        _run(
            run_agent_loop(
                provider=provider,
                model=_model(),
                context=context,
                tool_dispatcher=_dispatcher(tmp_path),
                max_turns=3,
            )
        )

    assert len(provider.calls) == 3
    assert len(context.messages) == 7


class _RecordingLoopObserver:
    def __init__(self) -> None:
        self.updates: list[tuple[int, list[type]]] = []

    async def on_context_updated(
        self,
        context: Context,
        *,
        turn_index: int,
    ) -> None:
        self.updates.append(
            (turn_index, [type(message) for message in context.messages])
        )


class _FailingLoopObserver:
    async def on_context_updated(
        self,
        context: Context,
        *,
        turn_index: int,
    ) -> None:
        raise RuntimeError("persistence failed")


def test_agent_loop_hook_after_each_context_update(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider(
        [
            AssistantMessage(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read",
                        arguments={"path": "hello.txt"},
                    )
                ]
            ),
            AssistantMessage(content="done"),
        ]
    )
    context = Context(messages=[UserMessage(content="read")])
    observer = _RecordingLoopObserver()

    _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_dispatcher=_dispatcher(tmp_path),
            observer=observer,
        )
    )

    assert [turn for turn, _ in observer.updates] == [1, 1, 2]
    assert [len(types) for _, types in observer.updates] == [2, 3, 4]
    assert observer.updates[0][1][-1] is AssistantMessage
    assert observer.updates[1][1][-1] is ToolResultMessage
    assert observer.updates[2][1][-1] is AssistantMessage


def test_agent_loop_hook_failure_is_not_silent(tmp_path: Path):
    provider = FakeProvider([AssistantMessage(content="done")])
    context = Context(messages=[UserMessage(content="hello")])

    with pytest.raises(RuntimeError, match="persistence failed"):
        _run(
            run_agent_loop(
                provider=provider,
                model=_model(),
                context=context,
                tool_dispatcher=_dispatcher(tmp_path),
                observer=_FailingLoopObserver(),
            )
        )
