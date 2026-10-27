
"""Minimal Pi-style agent loop.

核心流程：

Context
   ↓
Provider.stream()
   ↓
AssistantMessage
   ↓
是否包含 ToolCall？
   ├── 否 → 返回最终 AssistantMessage
   │
   └── 是
        ↓
   ToolExecutor
        ↓
   ToolResultMessage
        ↓
   append 回 Context
        ↓
   下一轮 Provider 调用
"""

from __future__ import annotations

from ai.context import Context
from ai.events import StreamEnd, StreamError
from ai.messages import AssistantMessage
from ai.models import Model
from ai.providers.base import Provider
from .hooks import AgentBudget, AgentLoopObserver
from .tool_dispatcher import ToolDispatcher

class AgentLoopError(RuntimeError):
    """Agent Loop 执行过程中发生的错误。"""


class MaxTurnsExceededError(AgentLoopError):
    """Agent 超过最大执行轮数仍未结束。"""


async def _stream_assistant_response(
    *,
    provider: Provider,
    model: Model,
    context: Context,
    tool_dispatcher: ToolDispatcher,
) -> AssistantMessage:
    """调用 Provider，并完整消费本轮 stream。"""

    tool_definitions = (
        tool_dispatcher.registry.definitions
    )

    final_message: AssistantMessage | None = None

    async for event in provider.stream(
        model,
        context,
        tool_definitions,
    ):
        if isinstance(event, StreamError):
            raise AgentLoopError(
                f"模型调用失败: {event.error}"
            )

        if isinstance(event, StreamEnd):
            # 不要立刻 return。
            # 先记录最终消息，让 provider 的 async generator
            # 自然运行到结束，以完成 HTTP stream cleanup。
            final_message = event.message

    if final_message is None:
        raise AgentLoopError(
            "Provider stream 在没有 StreamEnd / StreamError 的情况下结束"
        )

    return final_message

async def run_agent_loop(
    *,
    provider: Provider,
    model: Model,
    context: Context,
    tool_dispatcher: ToolDispatcher,
    max_turns: int = 20,
    observer: AgentLoopObserver | None = None,
    budget: AgentBudget | None = None,
) -> AssistantMessage:
    """运行最小 Agent Loop。

    一个 turn 定义为：

        一次模型调用
        +
        该模型响应要求执行的所有工具

    流程：

        1. 调用模型
        2. 将 AssistantMessage 写入 Context
        3. 如果没有 ToolCall，说明本次 Agent 完成
        4. 如果存在 ToolCall，则依次执行
        5. ToolResultMessage 写回 Context
        6. 开始下一轮模型调用

    Args:
        provider:
            AI Provider，例如 OpenAIProvider。

        model:
            当前模型配置。

        context:
            当前完整对话上下文。
            传入前一般已经包含 UserMessage。

        tool_dispatcher:
            当前 Agent 的 Tool Dispatcher。

        max_turns:
            最大模型调用轮数，用于防止模型无限调用工具。

    Returns:
        最终没有工具调用的 AssistantMessage。

    Raises:
        AgentLoopError:
            Provider 调用失败等 Runtime 错误。

        MaxTurnsExceededError:
            超过 max_turns 仍没有得到最终回答。
    """

    if max_turns <= 0:
        raise ValueError("max_turns 必须大于 0")

    if budget is not None:
        tool_dispatcher.set_budget(budget)

    for _turn in range(max_turns):
        if budget is not None:
            budget.consume_turn()
            budget.consume_model_call()
        # --------------------------------------------------
        # 1. 调用模型，得到这一轮完整 AssistantMessage
        # --------------------------------------------------

        request = _stream_assistant_response(
            provider=provider,
            model=model,
            context=context,
            tool_dispatcher=tool_dispatcher,
        )
        assistant = await budget.run(request) if budget is not None else await request

        # --------------------------------------------------
        # 2. AssistantMessage 必须进入对话历史
        # --------------------------------------------------
        #
        # 即使它只是：
        #
        # AssistantMessage(
        #     content="",
        #     tool_calls=[...],
        # )
        #
        # 也必须保留。
        #
        # 下一轮发送 ToolResultMessage 时，
        # Provider 需要看到：
        #
        # user
        # assistant(tool_call)
        # tool(result)
        #
        # 这个完整顺序。
        # --------------------------------------------------

        context.messages.append(assistant)

        if observer is not None:
            await observer.on_context_updated(
                context,
                turn_index=_turn + 1,
            )

        # --------------------------------------------------
        # 3. 没有 ToolCall → Agent 完成
        # --------------------------------------------------

        if not assistant.tool_calls:
            return assistant

        # --------------------------------------------------
        # 4. 执行当前 AssistantMessage 请求的所有工具
        # --------------------------------------------------
        #
        # 第一版故意串行执行。
        #
        # 后面再升级成：
        #
        # asyncio.gather(...)
        #
        # 或 TaskGroup，实现 Pi 风格并行 Tool execution。
        # --------------------------------------------------

        tool_request = tool_dispatcher.dispatch_many(assistant.tool_calls)
        results = (
            await budget.run(tool_request)
            if budget is not None
            else await tool_request
        )

        for result in results:

            # ----------------------------------------------
            # 5. ToolResultMessage 回写 Context
            # ----------------------------------------------

            context.messages.append(result)

            if observer is not None:
                await observer.on_context_updated(
                    context,
                    turn_index=_turn + 1,
                )

        # --------------------------------------------------
        # 6. 回到 for 循环顶部
        #
        # 下一轮 Provider 会看到：
        #
        # UserMessage
        # AssistantMessage(tool_calls)
        # ToolResultMessage
        #
        # 然后模型根据工具结果继续回答。
        # --------------------------------------------------

    raise MaxTurnsExceededError(
        f"Agent 已达到最大轮数 max_turns={max_turns}，"
        "但模型仍在继续请求工具调用"
    )
