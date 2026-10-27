"""Deterministic V7 replan triggers and revision ownership."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from coding_agent.agents.models import EvidenceBundle, TaskPlan, VerificationDecision
from coding_agent.errors import (
    CodingAgentError,
    ErrorCode,
    ReplanLimitExceededError,
)


class ReplanTrigger(str, Enum):
    STALE_VERSION = "stale_version"
    NO_MATCH = "no_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    TARGET_MISSING = "target_missing"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NEW_ERROR = "new_error"
    ACCEPTANCE_FAILURE = "acceptance_failure"
    INVALID_ASSUMPTION = "invalid_assumption"


class ReplanAssessment(BaseModel):
    should_replan: bool
    triggers: list[ReplanTrigger] = Field(default_factory=list)
    reason: str


class ReplanController:
    """Own the finite replan policy; the model never selects its revision."""

    def __init__(self, *, max_replans: int = 3) -> None:
        if max_replans < 0:
            raise ValueError("max_replans 不能为负数")
        self.max_replans = max_replans

    def assess(
        self,
        *,
        error: CodingAgentError | None = None,
        evidence: EvidenceBundle | None = None,
        verification: VerificationDecision | None = None,
        target_missing: bool = False,
        new_error_detected: bool = False,
        assumption_invalid: bool = False,
    ) -> ReplanAssessment:
        triggers: list[ReplanTrigger] = []
        error_mapping = {
            ErrorCode.STALE_VERSION: ReplanTrigger.STALE_VERSION,
            ErrorCode.NO_MATCH: ReplanTrigger.NO_MATCH,
            ErrorCode.AMBIGUOUS_MATCH: ReplanTrigger.AMBIGUOUS_MATCH,
            ErrorCode.FILE_NOT_FOUND: ReplanTrigger.TARGET_MISSING,
        }
        if error is not None and error.code in error_mapping:
            triggers.append(error_mapping[error.code])
        if target_missing:
            triggers.append(ReplanTrigger.TARGET_MISSING)
        if evidence is not None and evidence.unresolved_questions:
            triggers.append(ReplanTrigger.INSUFFICIENT_EVIDENCE)
        if new_error_detected:
            triggers.append(ReplanTrigger.NEW_ERROR)
        if verification is not None and (
            verification.should_replan or verification.failed_criteria
        ):
            triggers.append(ReplanTrigger.ACCEPTANCE_FAILURE)
        if assumption_invalid:
            triggers.append(ReplanTrigger.INVALID_ASSUMPTION)

        triggers = list(dict.fromkeys(triggers))
        return ReplanAssessment(
            should_replan=bool(triggers),
            triggers=triggers,
            reason=(
                ", ".join(trigger.value for trigger in triggers)
                if triggers
                else "No replan trigger was detected."
            ),
        )

    def advance_revision(
        self,
        candidate: TaskPlan,
        *,
        previous: TaskPlan,
        replan_count: int,
    ) -> TaskPlan:
        self.ensure_allowed(replan_count)
        return candidate.model_copy(update={"revision": previous.revision + 1})

    def ensure_allowed(self, replan_count: int) -> None:
        if replan_count < 0:
            raise ValueError("replan_count 不能为负数")
        if replan_count >= self.max_replans:
            raise ReplanLimitExceededError(max_replans=self.max_replans)
