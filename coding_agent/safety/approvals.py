"""CLI approval handler."""

from __future__ import annotations

import asyncio

from agent.approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
)


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

        print(
            f"参数: {request.arguments}"
        )

        answer = await asyncio.to_thread(
            input,
            "是否允许？[y/N]: ",
        )

        if answer.strip().lower() in {
            "y",
            "yes",
        }:
            return ApprovalDecision.APPROVE

        return ApprovalDecision.DENY