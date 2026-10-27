"""V7 Stop Rule A: plan and semantic change-intent contracts."""

import pytest
from pydantic import ValidationError

from coding_agent.agents.models import (
    ChangeIntent,
    PlanStep,
    PlanStepStatus,
    TaskPlan,
)


def _step(step_id: str = "inspect") -> PlanStep:
    return PlanStep(
        id=step_id,
        objective="Inspect the implementation",
        target_files=["src/app.py"],
        evidence_queries=["calculate_total"],
        acceptance_criteria=["Relevant definition is located"],
    )


def test_plan_valid():
    plan = TaskPlan(
        objective="Rename calculate_total safely",
        constraints=["Keep the compatibility wrapper"],
        steps=[_step(), _step("edit")],
        acceptance_criteria=["All tests pass"],
    )

    assert plan.revision == 1
    assert plan.steps[0].target_files == ["src/app.py"]


def test_plan_requires_steps():
    with pytest.raises(ValidationError):
        TaskPlan(
            objective="Goal",
            steps=[],
            acceptance_criteria=["Pass"],
        )


def test_plan_requires_acceptance_criteria():
    with pytest.raises(ValidationError):
        TaskPlan(
            objective="Goal",
            steps=[_step()],
            acceptance_criteria=[],
        )


def test_plan_duplicate_step_id_rejected():
    with pytest.raises(ValidationError, match="不能重复"):
        TaskPlan(
            objective="Goal",
            steps=[_step("same"), _step("same")],
            acceptance_criteria=["Pass"],
        )


@pytest.mark.parametrize("revision", [0, -1])
def test_plan_revision_positive(revision: int):
    with pytest.raises(ValidationError):
        TaskPlan(
            objective="Goal",
            steps=[_step()],
            acceptance_criteria=["Pass"],
            revision=revision,
        )


def test_plan_step_status_default_pending():
    assert _step().status == PlanStepStatus.PENDING


def test_plan_rejects_more_than_ten_steps():
    with pytest.raises(ValidationError):
        TaskPlan(
            objective="Goal",
            steps=[_step(str(index)) for index in range(11)],
            acceptance_criteria=["Pass"],
        )


def test_change_intent_is_semantic_not_patch_syntax():
    intent = ChangeIntent(
        objective="Fix add behavior",
        target_files=["src/math.py"],
        requirements=["Return the sum"],
        invariants=["Keep the public signature"],
        acceptance_criteria=["Math tests pass"],
    )

    assert "old_text" not in ChangeIntent.model_fields
    assert "new_text" not in ChangeIntent.model_fields


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", r"C:\outside.py"])
def test_plan_and_intent_reject_non_workspace_relative_targets(path: str):
    with pytest.raises(ValidationError):
        PlanStep(
            id="unsafe",
            objective="Unsafe",
            target_files=[path],
        )

    with pytest.raises(ValidationError):
        ChangeIntent(
            objective="Unsafe",
            target_files=[path],
            acceptance_criteria=["Never escape workspace"],
        )
