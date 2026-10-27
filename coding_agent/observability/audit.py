"""Minimal redacted JSONL audit sink for security-relevant decisions."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.observability.redaction import redact


_AUDITED = {
    AgentEventType.TOOL_APPROVED, AgentEventType.TOOL_DENIED,
    AgentEventType.POLICY_EVALUATED, AgentEventType.APPROVAL_REQUESTED,
    AgentEventType.APPROVAL_RESOLVED, AgentEventType.AUTO_CLASSIFIED,
    AgentEventType.MUTATION_APPLIED, AgentEventType.ROLLBACK_STARTED,
    AgentEventType.ROLLBACK_COMPLETED, AgentEventType.ROLLBACK_FAILED,
    AgentEventType.SANDBOX_VIOLATION,
}
_PERMISSION_EVENTS = {
    AgentEventType.TOOL_APPROVED, AgentEventType.TOOL_DENIED,
    AgentEventType.POLICY_EVALUATED, AgentEventType.APPROVAL_REQUESTED,
    AgentEventType.APPROVAL_RESOLVED, AgentEventType.AUTO_CLASSIFIED,
}
_PERMISSION_FIELDS = {
    "tool_name", "target", "normalized_targets", "permission_decision",
    "decision", "rule_id", "matched_rule_ids", "operations", "risk",
    "parser_status", "command_hash", "approval_scope", "duration_ms",
    "classifier_result", "classifier_stage", "classifier_input_tokens",
    "classifier_output_tokens", "error_code", "capabilities", "call_id",
}


class AuditRecord(BaseModel):
    event_id: str
    trace_id: str
    session_id: str
    actor: str = "user_or_policy"
    action: str
    tool: str | None = None
    target: str | None = None
    decision: str | None = None
    rule_id: str | None = None
    reason: str | None = None
    timestamp: datetime
    details: dict[str, Any]


class AuditSink:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)
        self._lock = asyncio.Lock()

    async def emit(self, event: AgentEvent) -> None:
        if event.type not in _AUDITED:
            return
        payload = redact(event.payload)
        if event.type in _PERMISSION_EVENTS:
            # Permission audit records are intentionally allowlisted.  Even a
            # future caller that accidentally supplies raw arguments/commands
            # cannot persist them here.
            payload = {
                key: value for key, value in payload.items()
                if key in _PERMISSION_FIELDS
            }
        normalized_targets = payload.get("normalized_targets") or []
        record = AuditRecord(
            event_id=event.event_id, trace_id=event.trace_id,
            session_id=event.session_id, action=event.type.value,
            tool=payload.get("tool_name"),
            target=payload.get("target") or (
                normalized_targets[0] if normalized_targets else None
            ),
            decision=payload.get("permission_decision") or payload.get("decision"),
            rule_id=payload.get("rule_id"), reason=payload.get("reason"),
            timestamp=event.timestamp,
            details={key: value for key, value in payload.items() if key not in {
                "tool_name", "target", "permission_decision", "decision", "rule_id", "reason"
            }},
        )
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        destination = self.root / f"audit-{day}.jsonl"
        async with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with destination.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
