"""V7 Stop Rule J: plan state persistence, compaction, and resume."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai.context import Context
from ai.models import Model
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.agents.models import PlanStep, PlanStepStatus, TaskPlan
from coding_agent.compaction import DeterministicCompactor
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.observations import ObservationStore
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.session import CodingSession, SessionSnapshot, SessionState
from coding_agent.session_store import SessionStore


def _plan(revision: int = 2) -> TaskPlan:
    return TaskPlan(
        objective="Repair calculator",
        constraints=["Keep public API"],
        steps=[
            PlanStep(
                id="done",
                objective="Inspect implementation",
                status=PlanStepStatus.COMPLETED,
            ),
            PlanStep(
                id="next",
                objective="Fix calculation",
                status=PlanStepStatus.PENDING,
            ),
            PlanStep(
                id="blocked",
                objective="Verify caller",
                status=PlanStepStatus.BLOCKED,
            ),
        ],
        acceptance_criteria=["Tests pass"],
        revision=revision,
    )


def _manager() -> ContextManager:
    return ContextManager(
        budget=ContextBudget(max_tokens=100_000, reserve_tokens=1_000)
    )


def test_session_plan_state_defaults_to_no_plan():
    state = SessionState(session_id="session-1", user_goal="goal")

    assert state.current_plan is None
    assert state.plan_revision == 0


def test_session_rejects_inconsistent_plan_revision():
    with pytest.raises(ValidationError):
        SessionState(
            session_id="session-1",
            user_goal="goal",
            current_plan=_plan(2),
            plan_revision=1,
        )


def test_plan_state_roundtrips_and_resume_restores_it(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    plan = _plan()
    store.save(
        SessionSnapshot(
            state=SessionState(
                session_id="session-1",
                user_goal="goal",
                current_plan=plan,
                plan_revision=plan.revision,
                latest_replan_reason="verification failed",
            ),
            context=Context(),
        )
    )

    resumed = CodingSession.resume(
        "session-1",
        provider=object(),  # no model call occurs in this persistence test
        model=Model(id="fake", provider="fake"),
        tool_dispatcher=ToolDispatcher(
            ToolRegistry([]), CodingPermissionEngine()
        ),
        session_store=store,
        context_manager=_manager(),
    )

    assert resumed.state.current_plan == plan
    assert resumed.state.plan_revision == 2
    assert resumed.state.latest_replan_reason == "verification failed"


def test_compaction_preserves_complete_plan_state():
    plan = _plan()
    state = SessionState(
        session_id="session-1",
        user_goal="goal",
        current_plan=plan,
        plan_revision=plan.revision,
        latest_replan_reason="old assumption invalid",
    )

    result = DeterministicCompactor(recent_groups=0).compact(
        context=Context(),
        session=state,
        observations=ObservationStore(),
    )

    summary = result.summary
    assert summary.plan_objective == "Repair calculator"
    assert summary.plan_constraints == ["Keep public API"]
    assert summary.plan_acceptance_criteria == ["Tests pass"]
    assert summary.completed_steps == ["done: Inspect implementation"]
    assert summary.pending_steps == ["next: Fix calculation"]
    assert summary.blocked_steps == ["blocked: Verify caller"]
    assert summary.plan_revision == 2
    assert summary.latest_replan_reason == "old assumption invalid"
    assert "Plan revision:\n- 2" in result.summary_text
