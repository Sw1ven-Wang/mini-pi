"""Tests for the minimal agent loop.

覆盖：

1. 无工具调用时，一轮直接返回最终 AssistantMessage。
2. 有工具调用时：
   LLM -> ToolCall -> ToolExecutor -> ToolResultMessage -> LLM。
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
from agent.tool_executor import ToolExecutor
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

        # 保存 snapshot，避免后面 context 被继续修改后影响历史断言。
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
    """运行 async coroutine。"""
    return asyncio.run(coro)


def _model() -> Model:
    return Model(
        id="fake-model",
        provider="fake",
    )


# ---------------------------------------------------------------------------
# 1. 没有工具调用
# ---------------------------------------------------------------------------


def test_agent_loop_returns_immediately_without_tool_calls():
    """模型第一轮没有 ToolCall 时，应直接结束。"""

    final_message = AssistantMessage(
        content="hello",
        tool_calls=[],
    )

    provider = FakeProvider(
        responses=[
            final_message,
        ]
    )

    context = Context(
        messages=[
            UserMessage(
                content="你好",
            )
        ]
    )

    executor = ToolExecutor([])

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_executor=executor,
        )
    )

    # 最终返回值正确
    assert result.content == "hello"
    assert result.tool_calls == []

    # 只调用了一次模型
    assert len(provider.calls) == 1

    # AssistantMessage 被写入 Context
    assert len(context.messages) == 2

    assert isinstance(
        context.messages[0],
        UserMessage,
    )

    assert isinstance(
        context.messages[1],
        AssistantMessage,
    )

    assert context.messages[1].content == "hello"


# ---------------------------------------------------------------------------
# 2. ToolCall -> ToolResult -> 第二轮 LLM
# ---------------------------------------------------------------------------


def test_agent_loop_executes_tool_and_calls_model_again(
    tmp_path: Path,
):
    """完整验证：

    User
      ↓
    Assistant(tool_call=read)
      ↓
    ReadTool
      ↓
    ToolResultMessage
      ↓
    第二次 LLM
      ↓
    最终 AssistantMessage
    """

    # --------------------------------------------------
    # 准备真实文件
    # --------------------------------------------------

    file_path = tmp_path / "hello.txt"
    file_path.write_text(
        "hello mini pi",
        encoding="utf-8",
    )

    # --------------------------------------------------
    # 第一次模型返回 read ToolCall
    # --------------------------------------------------

    first_response = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                name="read",
                arguments={
                    "path": "hello.txt",
                },
            )
        ],
    )

    # --------------------------------------------------
    # 第二次模型给最终答案
    # --------------------------------------------------

    second_response = AssistantMessage(
        content="文件内容是 hello mini pi",
        tool_calls=[],
    )

    provider = FakeProvider(
        responses=[
            first_response,
            second_response,
        ]
    )

    read_tool = ReadTool(tmp_path)
    executor = ToolExecutor(
        [
            read_tool,
        ]
    )

    context = Context(
        messages=[
            UserMessage(
                content="请读取 hello.txt",
            )
        ]
    )

    # --------------------------------------------------
    # 运行 Agent Loop
    # --------------------------------------------------

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_executor=executor,
        )
    )

    # --------------------------------------------------
    # 最终结果
    # --------------------------------------------------

    assert result.content == "文件内容是 hello mini pi"
    assert result.tool_calls == []

    # --------------------------------------------------
    # Provider 应调用两次
    # --------------------------------------------------

    assert len(provider.calls) == 2

    # --------------------------------------------------
    # 最终 Context 顺序
    # --------------------------------------------------
    #
    # 0 UserMessage
    # 1 AssistantMessage(tool_call)
    # 2 ToolResultMessage
    # 3 AssistantMessage(final)
    # --------------------------------------------------

    assert len(context.messages) == 4

    assert isinstance(
        context.messages[0],
        UserMessage,
    )

    assert isinstance(
        context.messages[1],
        AssistantMessage,
    )

    assert isinstance(
        context.messages[2],
        ToolResultMessage,
    )

    assert isinstance(
        context.messages[3],
        AssistantMessage,
    )

    # --------------------------------------------------
    # 第一轮 assistant
    # --------------------------------------------------

    first_assistant = context.messages[1]

    assert first_assistant.content == ""
    assert len(first_assistant.tool_calls) == 1

    assert first_assistant.tool_calls[0].id == "call_1"
    assert first_assistant.tool_calls[0].name == "read"

    # --------------------------------------------------
    # ToolResult
    # --------------------------------------------------

    tool_result = context.messages[2]

    assert tool_result.tool_call_id == "call_1"
    assert tool_result.name == "read"
    assert tool_result.content == "hello mini pi"
    assert tool_result.is_error is False

    # --------------------------------------------------
    # 最终 Assistant
    # --------------------------------------------------

    final_assistant = context.messages[3]

    assert final_assistant.content == "文件内容是 hello mini pi"

    # --------------------------------------------------
    # 第二次模型调用时，必须已经看到 tool result
    # --------------------------------------------------

    second_call_messages = provider.calls[1]["messages"]

    assert len(second_call_messages) == 3

    assert isinstance(
        second_call_messages[0],
        UserMessage,
    )

    assert isinstance(
        second_call_messages[1],
        AssistantMessage,
    )

    assert isinstance(
        second_call_messages[2],
        ToolResultMessage,
    )

    assert (
        second_call_messages[2].content
        == "hello mini pi"
    )

    # --------------------------------------------------
    # Provider 应收到 read 的 ToolDefinition
    # --------------------------------------------------

    first_call_tools = provider.calls[0]["tools"]

    assert len(first_call_tools) == 1

    assert first_call_tools[0].name == "read"


# ---------------------------------------------------------------------------
# 3. 工具异常也应该回灌模型
# ---------------------------------------------------------------------------


def test_agent_loop_feeds_tool_error_back_to_model(
    tmp_path: Path,
):
    """ReadTool 文件不存在时：

    ReadTool raises
        ↓
    ToolExecutor
        ↓
    ToolResultMessage(is_error=True)
        ↓
    下一轮模型仍然可以看到错误并回答
    """

    first_response = AssistantMessage(
        content="",
        tool_calls=[
            ToolCall(
                id="call_missing",
                name="read",
                arguments={
                    "path": "missing.txt",
                },
            )
        ],
    )

    second_response = AssistantMessage(
        content="文件不存在。",
        tool_calls=[],
    )

    provider = FakeProvider(
        responses=[
            first_response,
            second_response,
        ]
    )

    executor = ToolExecutor(
        [
            ReadTool(tmp_path),
        ]
    )

    context = Context(
        messages=[
            UserMessage(
                content="读取 missing.txt",
            )
        ]
    )

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_executor=executor,
        )
    )

    assert result.content == "文件不存在。"

    assert len(provider.calls) == 2

    assert len(context.messages) == 4

    tool_result = context.messages[2]

    assert isinstance(
        tool_result,
        ToolResultMessage,
    )

    assert tool_result.tool_call_id == "call_missing"
    assert tool_result.name == "read"
    assert tool_result.is_error is True
    assert tool_result.content != ""

    # 第二次 LLM 必须能看到这个错误结果
    second_call_messages = provider.calls[1]["messages"]

    error_message = second_call_messages[-1]

    assert isinstance(
        error_message,
        ToolResultMessage,
    )

    assert error_message.is_error is True


# ---------------------------------------------------------------------------
# 4. 未知工具也应该回灌模型
# ---------------------------------------------------------------------------


def test_agent_loop_feeds_unknown_tool_error_back_to_model():
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

    provider = FakeProvider(
        responses=[
            first_response,
            second_response,
        ]
    )

    executor = ToolExecutor([])

    context = Context(
        messages=[
            UserMessage(
                content="执行一个不存在的工具",
            )
        ]
    )

    result = _run(
        run_agent_loop(
            provider=provider,
            model=_model(),
            context=context,
            tool_executor=executor,
        )
    )

    assert result.content == "这个工具不可用。"

    tool_result = context.messages[2]

    assert isinstance(
        tool_result,
        ToolResultMessage,
    )

    assert tool_result.tool_call_id == "call_unknown"
    assert tool_result.name == "does_not_exist"
    assert tool_result.is_error is True


# ---------------------------------------------------------------------------
# 5. max_turns
# ---------------------------------------------------------------------------


def test_agent_loop_stops_after_max_turns(
    tmp_path: Path,
):
    """如果模型一直调用工具，必须由 max_turns 强制停止。"""

    file_path = tmp_path / "hello.txt"
    file_path.write_text(
        "hello",
        encoding="utf-8",
    )

    # 每一轮都要求继续读取文件，
    # 永远不给最终文本答案。
    responses = [
        AssistantMessage(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"call_{i}",
                    name="read",
                    arguments={
                        "path": "hello.txt",
                    },
                )
            ],
        )
        for i in range(3)
    ]

    provider = FakeProvider(responses)

    executor = ToolExecutor(
        [
            ReadTool(tmp_path),
        ]
    )

    context = Context(
        messages=[
            UserMessage(
                content="一直读文件",
            )
        ]
    )

    with pytest.raises(MaxTurnsExceededError):
        _run(
            run_agent_loop(
                provider=provider,
                model=_model(),
                context=context,
                tool_executor=executor,
                max_turns=3,
            )
        )

    # exactly 3 次模型调用
    assert len(provider.calls) == 3

    # Context：
    #
    # user
    #
    # assistant 1
    # tool result 1
    #
    # assistant 2
    # tool result 2
    #
    # assistant 3
    # tool result 3
    #
    assert len(context.messages) == 7