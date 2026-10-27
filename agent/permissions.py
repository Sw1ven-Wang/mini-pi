"""Generic permission interfaces."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agent.tools import AgentTool


class PermissionDecisionType(
    str,
    Enum,
):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionDecision(BaseModel):
    """一次权限检查结果。"""

    decision: PermissionDecisionType

    reason: str

    rule_id: str

    normalized_target: str | None = None

    matched_rule_ids: list[str] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    risk: str | None = None
    normalized_targets: list[str] = Field(default_factory=list)
    parser_status: str | None = None
    approval_summary: str | None = None
    hard_deny: bool = False
    command_hash: str | None = None
    requires_strong_sandbox: bool = False
    classifier_result: str | None = None
    classifier_stage: str | None = None
    classifier_latency_ms: int = Field(default=0, ge=0)
    classifier_input_tokens: int = Field(default=0, ge=0)
    classifier_output_tokens: int = Field(default=0, ge=0)


class PermissionEngine(Protocol):
    """Agent Runtime 使用的权限检查接口。"""

    async def evaluate(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
    ) -> PermissionDecision:
        ...
