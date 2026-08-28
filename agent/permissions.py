"""Generic permission interfaces."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel

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


class PermissionEngine(Protocol):
    """Agent Runtime 使用的权限检查接口。"""

    async def evaluate(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
    ) -> PermissionDecision:
        ...