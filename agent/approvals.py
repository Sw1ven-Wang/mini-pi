"""Generic approval interfaces."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel


class ApprovalDecision(
    str,
    Enum,
):
    APPROVE = "approve"
    DENY = "deny"


class ApprovalRequest(BaseModel):
    """一次等待用户确认的操作。"""

    tool_name: str
    arguments: dict[str, Any]

    reason: str
    rule_id: str


class ApprovalHandler(Protocol):
    """Agent Runtime 的审批接口。"""

    async def request(
        self,
        request: ApprovalRequest,
    ) -> ApprovalDecision:
        ...