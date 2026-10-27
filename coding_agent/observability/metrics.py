"""Run metrics and an event-driven collector."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from coding_agent.observability.events import AgentEvent, AgentEventType


class RunMetrics(BaseModel):
    task_success: bool = False
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    editor_input_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    read_calls: int = Field(default=0, ge=0)
    search_calls: int = Field(default=0, ge=0)
    files_read: int = Field(default=0, ge=0)
    mutation_attempts: int = Field(default=0, ge=0)
    mutation_failures: int = Field(default=0, ge=0)
    stale_version_errors: int = Field(default=0, ge=0)
    no_match_errors: int = Field(default=0, ge=0)
    ambiguous_match_errors: int = Field(default=0, ge=0)
    verification_runs: int = Field(default=0, ge=0)
    verification_failures: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    compactions: int = Field(default=0, ge=0)
    permission_asks: int = Field(default=0, ge=0)
    permission_denies: int = Field(default=0, ge=0)
    rollback_count: int = Field(default=0, ge=0)
    recovery_failures: int = Field(default=0, ge=0)
    process_timeouts: int = Field(default=0, ge=0)
    command_policy_blocks: int = Field(default=0, ge=0)
    sandbox_violations: int = Field(default=0, ge=0)
    parser_fallbacks: int = Field(default=0, ge=0)
    policy_allows: int = Field(default=0, ge=0)
    policy_asks: int = Field(default=0, ge=0)
    policy_denies: int = Field(default=0, ge=0)
    approval_interruptions: int = Field(default=0, ge=0)
    auto_classifier_calls: int = Field(default=0, ge=0)
    auto_allows: int = Field(default=0, ge=0)
    auto_blocks: int = Field(default=0, ge=0)
    auto_fallbacks: int = Field(default=0, ge=0)
    auto_degradations: int = Field(default=0, ge=0)
    auto_classifier_latency_ms: int = Field(default=0, ge=0)
    auto_classifier_input_tokens: int = Field(default=0, ge=0)
    auto_classifier_output_tokens: int = Field(default=0, ge=0)
    l2_start_failures: int = Field(default=0, ge=0)
    network_blocks: int = Field(default=0, ge=0)
    recovery_attempts: int = Field(default=0, ge=0)
    reconciliation_failures: int = Field(default=0, ge=0)
    approval_invalidations: int = Field(default=0, ge=0)
    approval_expirations: int = Field(default=0, ge=0)
    compensated_batches: int = Field(default=0, ge=0)
    avoided_effect_replays: int = Field(default=0, ge=0)
    acceptance_waits: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class MetricsSink:
    def __init__(self) -> None:
        self.by_trace: dict[str, RunMetrics] = {}
        self._lock = asyncio.Lock()

    async def emit(self, event: AgentEvent) -> None:
        async with self._lock:
            metrics = self.by_trace.setdefault(event.trace_id, RunMetrics())
            payload = event.payload
            if event.type == AgentEventType.MODEL_REQUEST_COMPLETED:
                metrics.model_calls += 1
                metrics.input_tokens += int(payload.get("input_tokens", 0))
                metrics.output_tokens += int(payload.get("output_tokens", 0))
            elif event.type == AgentEventType.TOOL_REQUESTED:
                metrics.tool_calls += 1
            elif event.type == AgentEventType.TOOL_COMPLETED:
                if payload.get("tool_name") == "read":
                    metrics.read_calls += 1
                elif payload.get("tool_name") == "search":
                    metrics.search_calls += 1
            elif event.type == AgentEventType.TOOL_APPROVED:
                if payload.get("permission_decision") == "ask":
                    metrics.permission_asks += 1
            elif event.type == AgentEventType.POLICY_EVALUATED:
                decision = payload.get("permission_decision")
                if decision == "allow":
                    metrics.policy_allows += 1
                elif decision == "ask":
                    metrics.policy_asks += 1
                elif decision == "deny":
                    metrics.policy_denies += 1
                if payload.get("parser_status") in {"error", "partial", "unavailable"}:
                    metrics.parser_fallbacks += 1
            elif event.type == AgentEventType.APPROVAL_REQUESTED:
                metrics.approval_interruptions += 1
            elif event.type == AgentEventType.AUTO_CLASSIFIED:
                metrics.auto_classifier_calls += 1
                metrics.auto_classifier_latency_ms += int(payload.get("duration_ms", 0))
                metrics.auto_classifier_input_tokens += int(
                    payload.get("classifier_input_tokens", 0)
                )
                metrics.auto_classifier_output_tokens += int(
                    payload.get("classifier_output_tokens", 0)
                )
                rule_id = payload.get("rule_id")
                if rule_id == "auto_classifier_allow":
                    metrics.auto_allows += 1
                elif rule_id == "auto_classifier_block":
                    metrics.auto_blocks += 1
                elif rule_id == "auto_degraded_to_default":
                    metrics.auto_degradations += 1
                else:
                    metrics.auto_fallbacks += 1
            elif event.type == AgentEventType.TOOL_DENIED:
                metrics.permission_denies += 1
                if payload.get("error_code") == "command_policy_blocked":
                    metrics.command_policy_blocks += 1
            elif event.type == AgentEventType.TOOL_FAILED:
                code = payload.get("error_code")
                if code == "stale_version":
                    metrics.stale_version_errors += 1
                elif code == "no_match":
                    metrics.no_match_errors += 1
                elif code == "ambiguous_match":
                    metrics.ambiguous_match_errors += 1
                elif code == "process_timeout":
                    metrics.process_timeouts += 1
            elif event.type == AgentEventType.MUTATION_PLANNED:
                metrics.mutation_attempts += 1
            elif event.type == AgentEventType.MUTATION_ROLLED_BACK:
                metrics.mutation_failures += 1
                metrics.rollback_count += 1
                if payload.get("recovery_failure"):
                    metrics.recovery_failures += 1
            elif event.type == AgentEventType.ROLLBACK_COMPLETED:
                metrics.rollback_count += 1
            elif event.type == AgentEventType.ROLLBACK_FAILED:
                metrics.recovery_failures += 1
            elif event.type == AgentEventType.SANDBOX_VIOLATION:
                count = max(1, int(payload.get("count", 1)))
                metrics.sandbox_violations += count
                if payload.get("kind") == "network":
                    metrics.network_blocks += count
                if payload.get("kind") == "l2_start":
                    metrics.l2_start_failures += count
            elif event.type == AgentEventType.VERIFICATION_STARTED:
                metrics.verification_runs += 1
            elif event.type == AgentEventType.VERIFICATION_COMPLETED:
                if not payload.get("passed", False):
                    metrics.verification_failures += 1
            elif event.type == AgentEventType.PLAN_REVISED:
                metrics.replans += 1
            elif event.type == AgentEventType.CONTEXT_COMPACTED:
                metrics.compactions += 1
            elif event.type == AgentEventType.TASK_COMPLETED:
                metrics.task_success = bool(payload.get("passed", False))
                metrics.duration_ms = int(payload.get("duration_ms", 0))

    def snapshot(self, trace_id: str) -> RunMetrics:
        return self.by_trace.get(trace_id, RunMetrics()).model_copy(deep=True)
