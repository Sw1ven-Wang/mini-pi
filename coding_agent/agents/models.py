"""Structured contracts shared by V7 planner/viewer/editor/verifier phases."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from coding_agent.diagnostics.models import Diagnostic
from coding_agent.filesystem.versions import FileVersion
from coding_agent.observations import normalize_observation_path
from coding_agent.search.models import SearchMatch


MAX_PLAN_STEPS = 10


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class PlanStep(BaseModel):
    id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    target_files: list[str] = Field(default_factory=list)
    evidence_queries: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: PlanStepStatus = PlanStepStatus.PENDING

    @field_validator("target_files")
    @classmethod
    def _normalize_target_files(cls, value: list[str]) -> list[str]:
        return [normalize_observation_path(path) for path in value]


class TaskPlan(BaseModel):
    objective: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(
        min_length=1,
        max_length=MAX_PLAN_STEPS,
    )
    acceptance_criteria: list[str] = Field(min_length=1)
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_step_ids(self) -> "TaskPlan":
        ids = [step.id for step in self.steps]

        if len(ids) != len(set(ids)):
            raise ValueError("plan step id 不能重复")

        return self


class ChangeIntent(BaseModel):
    """Semantic change request; deliberately contains no patch syntax."""

    objective: str = Field(min_length=1)
    target_files: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(min_length=1)

    @field_validator("target_files")
    @classmethod
    def _normalize_target_files(cls, value: list[str]) -> list[str]:
        return [normalize_observation_path(path) for path in value]


class PlannerContext(BaseModel):
    """Minimal planner view; it intentionally has no chat-history field."""

    constraints: list[str] = Field(default_factory=list)
    session_summary: str | None = None
    repository_map_summary: str = ""
    unresolved_diagnostics: list[Diagnostic] = Field(default_factory=list)


class EvidenceSnippet(BaseModel):
    """A source excerpt tied to the exact file version that produced it."""

    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    content: str
    version: FileVersion

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return normalize_observation_path(value)

    @model_validator(mode="after")
    def _validate_line_range(self) -> "EvidenceSnippet":
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line 与 end_line 必须同时提供或同时省略")

        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("end_line 不能小于 start_line")

        return self


class EvidenceBundle(BaseModel):
    """Bounded, workspace-relative evidence gathered for one edit phase."""

    snippets: list[EvidenceSnippet] = Field(default_factory=list)
    search_results: list[SearchMatch] = Field(default_factory=list)
    repository_notes: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)


class EditorRequest(BaseModel):
    """Minimal editor input; operation selection is owned by the host."""

    intent: ChangeIntent
    evidence: EvidenceBundle


class VerificationDecision(BaseModel):
    passed: bool
    failed_criteria: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    should_replan: bool
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_decision(self) -> "VerificationDecision":
        if self.passed == self.should_replan:
            raise ValueError("passed 与 should_replan 必须互为相反状态")
        if self.passed and self.failed_criteria:
            raise ValueError("passed decision 不能包含 failed_criteria")
        return self
