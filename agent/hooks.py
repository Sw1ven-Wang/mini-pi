"""Generic runtime hook protocols.

agent 层不得 import coding_agent，保持依赖方向：

    ai
    ↑
    agent
    ↑
    coding_agent
"""

from __future__ import annotations

from typing import Protocol

from ai.context import Context
from agent.tools import AgentToolResult


class ToolResultObserver(Protocol):
    """在 tool.execute 成功之后被调用，用于更新 Host 侧观察状态。"""

    async def on_tool_result(
        self,
        *,
        tool_name: str,
        arguments: dict,
        result: AgentToolResult,
    ) -> None:
        ...


class AgentLoopObserver(Protocol):
    """AgentLoop 在 context 更新后被调用，用于持久化 session。"""

    async def on_context_updated(
        self,
        context: Context,
        *,
        turn_index: int,
    ) -> None:
        ...


class ToolLifecycleObserver(Protocol):
    """Generic hook; coding_agent adapters decide how lifecycle data is stored."""

    async def on_tool_event(
        self,
        *,
        stage: str,
        tool_name: str,
        call_id: str,
        capabilities: list[str],
        permission_decision: str | None = None,
        rule_id: str | None = None,
        duration_ms: int | None = None,
        error_code: str | None = None,
        matched_rule_ids: list[str] | None = None,
        risk: str | None = None,
        parser_status: str | None = None,
        approval_summary: str | None = None,
        command_hash: str | None = None,
        approval_scope: str | None = None,
        reason: str | None = None,
        operations: list[dict] | None = None,
        normalized_targets: list[str] | None = None,
        classifier_result: str | None = None,
        classifier_stage: str | None = None,
        classifier_input_tokens: int | None = None,
        classifier_output_tokens: int | None = None,
    ) -> None: ...


class ToolBudget(Protocol):
    def consume_tool_call(self, count: int = 1) -> None: ...


class AgentBudget(ToolBudget, Protocol):
    """Budget surface used by the dependency-light agent loop."""

    def consume_turn(self, count: int = 1) -> None: ...

    def consume_model_call(self, count: int = 1) -> None: ...

    async def run(self, awaitable): ...
