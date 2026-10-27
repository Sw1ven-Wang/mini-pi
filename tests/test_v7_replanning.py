"""V7 Stop Rule I: deterministic triggers and bounded plan revisions."""

import pytest

from coding_agent.agents.models import (
    EvidenceBundle,
    PlanStep,
    TaskPlan,
    VerificationDecision,
)
from coding_agent.errors import (
    AmbiguousMatchError,
    FileNotFoundAgentError,
    NoMatchError,
    ReplanLimitExceededError,
    StaleVersionError,
)
from coding_agent.replanning import ReplanController, ReplanTrigger


def _plan(revision: int = 1) -> TaskPlan:
    return TaskPlan(
        objective="Fix calculation",
        steps=[
            PlanStep(
                id="edit",
                objective="Fix implementation",
                acceptance_criteria=["Implementation is correct"],
            )
        ],
        acceptance_criteria=["Tests pass"],
        revision=revision,
    )


@pytest.mark.parametrize(
    ("error", "trigger"),
    [
        (
            StaleVersionError(expected="old", current="new"),
            ReplanTrigger.STALE_VERSION,
        ),
        (NoMatchError("missing"), ReplanTrigger.NO_MATCH),
        (
            AmbiguousMatchError(old_text="x", occurrences=2),
            ReplanTrigger.AMBIGUOUS_MATCH,
        ),
        (FileNotFoundAgentError("missing.py"), ReplanTrigger.TARGET_MISSING),
    ],
)
def test_replan_for_mutation_and_missing_target_errors(error, trigger):
    assessment = ReplanController().assess(error=error)

    assert assessment.should_replan is True
    assert assessment.triggers == [trigger]


def test_replan_for_evidence_verification_error_and_invalid_assumption():
    decision = VerificationDecision(
        passed=False,
        failed_criteria=["Tests pass"],
        should_replan=True,
        reason="new ERROR diagnostics: 1",
    )
    assessment = ReplanController().assess(
        evidence=EvidenceBundle(unresolved_questions=["Missing caller evidence"]),
        verification=decision,
        new_error_detected=True,
        assumption_invalid=True,
    )

    assert assessment.triggers == [
        ReplanTrigger.INSUFFICIENT_EVIDENCE,
        ReplanTrigger.NEW_ERROR,
        ReplanTrigger.ACCEPTANCE_FAILURE,
        ReplanTrigger.INVALID_ASSUMPTION,
    ]


def test_no_trigger_keeps_current_plan():
    assessment = ReplanController().assess(evidence=EvidenceBundle())

    assert assessment.should_replan is False
    assert assessment.triggers == []


def test_host_advances_revision_monotonically():
    revised = ReplanController().advance_revision(
        _plan(99),
        previous=_plan(1),
        replan_count=0,
    )

    assert revised.revision == 2


def test_replan_limit_is_three_by_default():
    with pytest.raises(ReplanLimitExceededError):
        ReplanController().advance_revision(
            _plan(),
            previous=_plan(4),
            replan_count=3,
        )


def test_replan_limit_can_be_checked_before_calling_model():
    with pytest.raises(ReplanLimitExceededError):
        ReplanController().ensure_allowed(3)
