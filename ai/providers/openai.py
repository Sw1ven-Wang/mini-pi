"""OpenAI-compatible provider implementation (streaming Chat Completions)."""

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from ..context import Context
from ..events import (
    AIEvent,
    StreamEnd,
    StreamError,
    StreamStart,
    TextDelta,
    ToolCallReady,
)
from ..messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from ..models import Model
from ..tools import ToolDefinition
from .base import Provider


class OpenAIProvider(Provider):
    """把 OpenAI Chat Completions API 的流式返回转换为 AIEvent 序列。

    使用官方 Python SDK 的 :class:`AsyncOpenAI` 客户端，以 ``stream=True``
    发起请求，并将逐块返回的内容映射为 ``ai.events`` 里的事件。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        # 优先复用外部传入的 client（便于测试/自定义连接），否则新建。
        # api_key 为 None 时，SDK 会自动读取环境变量 OPENAI_API_KEY。
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        """发起流式请求，并把结果逐条产出为 AIEvent。

        事件顺序：``StreamStart`` → (若干 ``TextDelta`` / ``ToolCallStart``)
        → ``StreamEnd``；出错时以 ``StreamError`` 结束。
        """
        try:
            yield StreamStart()

            # 组装请求参数；没有工具时省略 tools 字段，避免传空数组报错。
            kwargs: dict[str, Any] = {
                "model": model.id,
                "messages": self._to_openai_messages(context),
                "stream": True,
            }
            if tools:
                kwargs["tools"] = self._to_openai_tools(tools)

            stream = await self._client.chat.completions.create(**kwargs)

            content_parts: list[str] = []
            # OpenAI 流式返回中，多个工具调用用 index 区分；每个调用的
            # arguments（JSON 字符串）分片到达，需按 index 累积拼接。
            call_buf: dict[int, dict[str, str]] = {}

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue

                if delta.content:
                    content_parts.append(delta.content)
                    yield TextDelta(delta=delta.content)

                if delta.tool_calls:
                    self._accumulate(delta.tool_calls, call_buf)

            # 流结束后，把累积到的工具调用组装为完整 ToolCall，
            # 并产出 ToolCallReady 事件，最后产出携带完整消息的 StreamEnd。
            tool_calls: list[ToolCall] = []
            for index in sorted(call_buf):
                call = self._assemble(call_buf[index])
                tool_calls.append(call)
                yield ToolCallReady(tool_call=call)

            yield StreamEnd(
                message=AssistantMessage(
                    content="".join(content_parts),
                    tool_calls=tool_calls,
                )
            )
        except Exception as exc:  # 任何异常都转成事件，交给上层处理
            yield StreamError(error=str(exc))

    # ---------- 消息 / 工具格式转换 ----------

    def _to_openai_messages(self, context: Context) -> list[dict[str, Any]]:
        """把内部 Context 转成 OpenAI 的 messages 列表格式。"""
        messages: list[dict[str, Any]] = []
        if context.system_prompt:
            messages.append({"role": "system", "content": context.system_prompt})
        for msg in context.messages:
            messages.append(self._message_to_openai(msg))
        return messages

    def _message_to_openai(self, msg: Message) -> dict[str, Any]:
        """把单条内部消息转成 OpenAI 的 message 字典。"""
        if isinstance(msg, UserMessage):
            return {"role": "user", "content": msg.content}

        if isinstance(msg, AssistantMessage):
            # 带 tool_calls 的 assistant 消息，content 必须为 null，否则报错。
            if msg.tool_calls:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in msg.tool_calls
                    ],
                }
            return {"role": "assistant", "content": msg.content}

        if isinstance(msg, ToolResultMessage):
            # 工具执行结果：用 tool_call_id 关联回之前的调用。
            return {
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "content": msg.content,
            }

        raise TypeError(f"不支持的内部消息类型: {type(msg)!r}")

    def _to_openai_tools(
        self,
        tools: list[ToolDefinition],
    ) -> list[dict[str, Any]]:
        """把内部 ToolDefinition 转成 OpenAI 的 tools 数组格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    # ---------- 流式工具调用的累积与组装 ----------

    @staticmethod
    def _accumulate(
        deltas: list[Any],
        call_buf: dict[int, dict[str, str]],
    ) -> None:
        """把本块到达的工具调用分片累加进 call_buf（按 index 区分）。"""
        for delta in deltas:
            index = delta.index
            buf = call_buf.setdefault(
                index, {"id": "", "name": "", "arguments": ""}
            )
            if delta.id and not buf["id"]:
                buf["id"] = delta.id

            fn = delta.function
            if fn is None:
                continue
            if fn.name and not buf["name"]:
                buf["name"] = fn.name
            if fn.arguments:
                buf["arguments"] += fn.arguments

    @staticmethod
    def _assemble(parts: dict[str, str]) -> ToolCall:
        """把累积到的原始分片组装成内部 ToolCall。"""
        return ToolCall(
            id=parts["id"],
            name=parts["name"],
            arguments=_parse_json(parts["arguments"]),
        )


def _parse_json(text: str) -> dict[str, Any]:
    """把 OpenAI 返回的 JSON 字符串参数解析为 dict。

    arguments 在接口里本质上是 JSON 字符串。若最终无法解析，说明数据已损坏，
    必须抛错（由 stream 转成 StreamError），绝不能静默返回空 dict——
    否则会把"数据损坏"伪装成"模型调用了空参数工具"，非常危险。
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"工具调用参数不是合法 JSON: {text!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"工具调用参数不是 JSON 对象: {text!r}")
    return parsed
