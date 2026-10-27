"""Reproducible metric comparison helpers for the V7 experiment."""

from __future__ import annotations

from pydantic import BaseModel

from coding_agent.orchestration import OrchestrationMetrics


class ExperimentComparison(BaseModel):
    baseline: OrchestrationMetrics
    v7: OrchestrationMetrics
    editor_context_reduction_percent: float


def compare_metrics(
    baseline: OrchestrationMetrics,
    v7: OrchestrationMetrics,
) -> ExperimentComparison:
    if baseline.editor_input_tokens <= 0:
        reduction = 0.0
    else:
        reduction = (
            1 - (v7.editor_input_tokens / baseline.editor_input_tokens)
        ) * 100
    return ExperimentComparison(
        baseline=baseline,
        v7=v7,
        editor_context_reduction_percent=round(reduction, 2),
    )


def render_metrics_table(comparison: ExperimentComparison) -> str:
    rows = [
        ("Task Success", comparison.baseline.task_success, comparison.v7.task_success),
        ("Model Calls", comparison.baseline.model_calls, comparison.v7.model_calls),
        ("Input Tokens (estimated)", comparison.baseline.input_tokens, comparison.v7.input_tokens),
        ("Output Tokens (estimated)", comparison.baseline.output_tokens, comparison.v7.output_tokens),
        ("Editor Input Tokens (estimated)", comparison.baseline.editor_input_tokens, comparison.v7.editor_input_tokens),
        ("Tool Calls", comparison.baseline.tool_calls, comparison.v7.tool_calls),
        ("Read Calls", comparison.baseline.read_calls, comparison.v7.read_calls),
        ("Search Calls", comparison.baseline.search_calls, comparison.v7.search_calls),
        ("Files Read", comparison.baseline.files_read, comparison.v7.files_read),
        ("Mutation Attempts", comparison.baseline.mutation_attempts, comparison.v7.mutation_attempts),
        ("NO_MATCH", comparison.baseline.no_match_count, comparison.v7.no_match_count),
        ("AMBIGUOUS_MATCH", comparison.baseline.ambiguous_match_count, comparison.v7.ambiguous_match_count),
        ("STALE_VERSION", comparison.baseline.stale_version_count, comparison.v7.stale_version_count),
        ("Verification Attempts", comparison.baseline.verification_attempts, comparison.v7.verification_attempts),
        ("Replan Count", comparison.baseline.replan_count, comparison.v7.replan_count),
        ("Total Duration (ms)", comparison.baseline.total_duration_ms, comparison.v7.total_duration_ms),
    ]
    lines = ["| Metric | V6 Baseline | V7 |", "|---|---:|---:|"]
    lines.extend(f"| {name} | {baseline} | {v7} |" for name, baseline, v7 in rows)
    return "\n".join(lines)
