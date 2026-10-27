"""Generic approval interfaces."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ApprovalDecision(
    str,
    Enum,
):
    APPROVE_ONCE = "approve_once"
    APPROVE_SESSION = "approve_session"
    DENY = "deny"

    # Backward-compatible alias. New handlers should return APPROVE_ONCE.
    APPROVE = "approve_once"


class ApprovalRequest(BaseModel):
    """一次等待用户确认的操作。"""

    tool_name: str
    arguments: dict[str, Any]

    reason: str
    rule_id: str
    matched_rule_ids: list[str] = Field(default_factory=list)
    risk: str | None = None
    normalized_targets: list[str] = Field(default_factory=list)
    summary: str | None = None
    command_hash: str | None = None
    preview_id: str | None = None


class ApprovalHandler(Protocol):
    """Agent Runtime 的审批接口。"""

    async def request(
        self,
        request: ApprovalRequest,
    ) -> ApprovalDecision:
        ...
