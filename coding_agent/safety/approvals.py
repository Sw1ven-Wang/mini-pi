"""CLI approval handler."""

from __future__ import annotations

import asyncio

from agent.approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
)
from coding_agent.observability.redaction import redact_text


class CLIApprovalHandler(
    ApprovalHandler
):
    """通过终端询问用户是否批准。"""

    async def request(
        self,
        request: ApprovalRequest,
    ) -> ApprovalDecision:

        print()
        print("=" * 60)
        print("需要用户批准")
        print("=" * 60)

        print(
            f"工具: {request.tool_name}"
        )

        print(
            f"原因: {request.reason}"
        )

        if request.summary:
            print(f"操作: {redact_text(request.summary)}")
        if request.risk:
            print(f"风险: {request.risk}")
        if request.normalized_targets:
            print(
                "目标: "
                + ", ".join(redact_text(item) for item in request.normalized_targets)
            )
        print(f"规则: {', '.join(request.matched_rule_ids or [request.rule_id])}")

        answer = await asyncio.to_thread(
            input,
            "是否允许？[y=一次/s=本会话/N]: ",
        )

        if answer.strip().lower() in {
            "y",
            "yes",
        }:
            return ApprovalDecision.APPROVE_ONCE

        if answer.strip().lower() in {"s", "session"}:
            return ApprovalDecision.APPROVE_SESSION

        return ApprovalDecision.DENY
