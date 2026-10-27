"""Future-backed approval adapter for a single local web task."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from agent.approvals import ApprovalDecision, ApprovalHandler, ApprovalRequest
from coding_agent.web.models import ApprovalView
from coding_agent.runtime.models import ApprovalStatus, ExecutionPaused


ApprovalCallback = Callable[[ApprovalView | None], Awaitable[None]]


class WebApprovalHandler(ApprovalHandler):
    def __init__(self, on_change: ApprovalCallback, *, durable=None) -> None:
        self._on_change = on_change
        self._pending_id: str | None = None
        self._pending: ApprovalView | None = None
        self._future: asyncio.Future[ApprovalDecision] | None = None
        self.durable = durable

    @property
    def pending(self) -> ApprovalView | None:
        if self.durable is not None:
            pending = self.durable.pending()
            return ApprovalView(approval_id=pending.approval_id, **pending.view) if pending else None
        return self._pending.model_copy(deep=True) if self._pending else None

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        if self.durable is not None:
            record = self.durable.request(request)
            if record.status == ApprovalStatus.APPROVED:
                return ApprovalDecision(record.decision)
            await self._on_change(self.pending)
            raise ExecutionPaused()
        if self._future is not None and not self._future.done():
            raise RuntimeError("a web task cannot hold multiple approvals")
        approval_id = str(uuid4())
        self._pending_id = approval_id
        self._pending = ApprovalView(
            approval_id=approval_id,
            tool_name=request.tool_name,
            reason=request.reason,
            rule_id=request.rule_id,
            matched_rule_ids=request.matched_rule_ids,
            risk=request.risk,
            normalized_targets=request.normalized_targets,
            summary=request.summary,
            preview_id=request.preview_id,
        )
        self._future = asyncio.get_running_loop().create_future()
        await self._on_change(self.pending)
        try:
            return await self._future
        finally:
            self._pending_id = None
            self._pending = None
            self._future = None
            await self._on_change(None)

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> None:
        if self.durable is not None:
            self.durable.resolve(approval_id, decision)
            return
        if self._pending_id != approval_id or self._future is None:
            raise KeyError("approval is unknown or expired")
        if self._future.done():
            raise RuntimeError("approval has already been resolved")
        self._future.set_result(decision)

    def deny_pending(self) -> None:
        if self.durable is not None:
            self.durable.invalidate_pending()
            return
        if self._future is not None and not self._future.done():
            self._future.set_result(ApprovalDecision.DENY)
