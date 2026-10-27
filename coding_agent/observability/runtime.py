"""Adapters from runtime hooks to safe AgentEvents."""

from __future__ import annotations

from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.tracing import TraceContext


class ToolEventObserver:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        session_id: str,
        trace: TraceContext,
    ) -> None:
        self.event_bus = event_bus
        self.session_id = session_id
        self.trace = trace
        self._spans: dict[str, TraceContext] = {}

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
    ) -> None:
        span = self._spans.setdefault(call_id, self.trace.child())
        event_type = {
            "requested": AgentEventType.TOOL_REQUESTED,
            "approved": AgentEventType.TOOL_APPROVED,
            "denied": AgentEventType.TOOL_DENIED,
            "started": AgentEventType.TOOL_STARTED,
            "completed": AgentEventType.TOOL_COMPLETED,
            "failed": AgentEventType.TOOL_FAILED,
            "policy_evaluated": AgentEventType.POLICY_EVALUATED,
            "approval_requested": AgentEventType.APPROVAL_REQUESTED,
            "approval_resolved": AgentEventType.APPROVAL_RESOLVED,
            "auto_classified": AgentEventType.AUTO_CLASSIFIED,
        }[stage]
        payload = {
            "tool_name": tool_name,
            "call_id": call_id,
            "capabilities": capabilities,
        }
        for key, value in {
            "permission_decision": permission_decision,
            "rule_id": rule_id,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "matched_rule_ids": matched_rule_ids,
            "risk": risk,
            "parser_status": parser_status,
            "approval_summary": approval_summary,
            "command_hash": command_hash,
            "approval_scope": approval_scope,
            "reason": reason,
            "operations": operations,
            "normalized_targets": normalized_targets,
            "classifier_result": classifier_result,
            "classifier_stage": classifier_stage,
            "classifier_input_tokens": classifier_input_tokens,
            "classifier_output_tokens": classifier_output_tokens,
        }.items():
            if value is not None:
                payload[key] = value
        await self.event_bus.emit(
            span.event(
                session_id=self.session_id,
                type=event_type,
                payload=payload,
            )
        )
        if stage in {"completed", "failed", "denied"}:
            self._spans.pop(call_id, None)


class MutationEventObserver:
    """Maps recovery lifecycle callbacks onto the active task trace."""

    def __init__(self, *, event_bus: EventBus, session_id: str, trace: TraceContext) -> None:
        self.event_bus = event_bus
        self.session_id = session_id
        self.span = trace.child()

    async def on_mutation_event(self, *, stage: str, payload: dict) -> None:
        event_type = {
            "rollback_started": AgentEventType.ROLLBACK_STARTED,
            "rollback_completed": AgentEventType.ROLLBACK_COMPLETED,
            "rollback_failed": AgentEventType.ROLLBACK_FAILED,
        }[stage]
        await self.event_bus.emit(self.span.event(
            session_id=self.session_id, type=event_type, payload=payload
        ))
