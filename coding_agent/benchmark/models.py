"""Stable benchmark input and result schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from coding_agent.observability.metrics import RunMetrics


class BenchmarkTask(BaseModel):
    task_id: str
    profile: str = "V10"
    category: str = "general"
    repo_fixture: str
    prompt: str
    acceptance_commands: list[str]
    forbidden_paths: list[str] = Field(default_factory=list)
    expected_changed_files: list[str] | None = None
    max_turns: int = Field(default=30, gt=0)


class BenchmarkExecution(BaseModel):
    success: bool
    verification_passed: bool = False
    changed_files: list[str] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    recovery_attempted: bool = False
    recovery_succeeded: bool = False


class BenchmarkResult(BaseModel):
    task_id: str
    profile: str = "V10"
    success: bool
    verification_passed: bool
    security_violations: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    mutation_attempts: int = Field(ge=0)
    mutation_failures: int = Field(default=0, ge=0)
    replans: int = Field(ge=0)
    stale_errors: int = Field(ge=0)
    no_match_errors: int = Field(ge=0)
    recovery_attempted: bool = False
    recovery_succeeded: bool = False
    trace_id: str
    error: str | None = None
    policy_asks: int = Field(default=0, ge=0)
    approval_interruptions: int = Field(default=0, ge=0)
    auto_classifier_calls: int = Field(default=0, ge=0)
    auto_allows: int = Field(default=0, ge=0)
    auto_blocks: int = Field(default=0, ge=0)
    parser_fallbacks: int = Field(default=0, ge=0)
    sandbox_violations: int = Field(default=0, ge=0)
    auto_classifier_latency_ms: int = Field(default=0, ge=0)
    auto_classifier_input_tokens: int = Field(default=0, ge=0)
    auto_classifier_output_tokens: int = Field(default=0, ge=0)
