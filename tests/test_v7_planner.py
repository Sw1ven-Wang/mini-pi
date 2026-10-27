"""V7 Stop Rule B: isolated, no-tool single-model Planner."""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage
from ai.models import Model
from ai.tools import ToolDefinition
from coding_agent.agents.models import PlannerContext
from coding_agent.agents.planner import Planner
from coding_agent.diagnostics.models import Diagnostic, DiagnosticSeverity
from coding_agent.errors import PlanningError


class _FakeProvider:
    def __init__(self, content: str, *, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.calls: list[dict] = []

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        self.calls.append(
            {"model": model, "context": context, "tools": tools}
        )
        yield StreamStart()
        yield StreamEnd(
            message=AssistantMessage(
                content=self.content,
                tool_calls=self.tool_calls,
            )
        )


def _run(coro):
    return asyncio.run(coro)


def _model() -> Model:
    return Model(id="shared-model", provider="fake")


def _valid_plan() -> dict:
    return {
        "objective": "Fix the calculation",
        "constraints": ["Keep public API"],
        "steps": [
            {
                "id": "step-1",
                "objective": "Inspect definition and tests",
                "target_files": ["src/math.py"],
                "evidence_queries": ["calculate_total"],
                "acceptance_criteria": ["Definition and calls are known"],
            }
        ],
        "acceptance_criteria": ["All tests pass"],
        "revision": 1,
    }


def test_planner_returns_valid_task_plan():
    provider = _FakeProvider(json.dumps(_valid_plan()))
    planner = Planner(provider, _model())

    plan = _run(
        planner.create_plan(
            goal="Fix calculation",
            context=PlannerContext(),
        )
    )

    assert plan.objective == "Fix the calculation"
    assert plan.steps[0].evidence_queries == ["calculate_total"]


def test_planner_uses_same_model_and_has_no_tools():
    provider = _FakeProvider(json.dumps(_valid_plan()))
    model = _model()
    planner = Planner(provider, model)

    _run(
        planner.create_plan(
            goal="Fix calculation",
            context=PlannerContext(),
        )
    )

    assert provider.calls[0]["model"] is model
    assert provider.calls[0]["tools"] == []


def test_planner_context_is_minimal_and_excludes_chat_history():
    provider = _FakeProvider(json.dumps(_valid_plan()))
    planner_context = PlannerContext(
        constraints=["Do not modify wrapper.py"],
        session_summary="Previous compact summary",
        repository_map_summary="src/math.py: calculate_total",
        unresolved_diagnostics=[
            Diagnostic(
                message="assertion failed",
                severity=DiagnosticSeverity.ERROR,
                source="pytest",
            )
        ],
    )

    _run(
        Planner(provider, _model()).create_plan(
            goal="Fix calculation",
            context=planner_context,
        )
    )

    sent = provider.calls[0]["context"]
    payload = json.loads(sent.messages[0].content)
    assert payload["constraints"] == ["Do not modify wrapper.py"]
    assert "messages" not in payload
    assert "chat_history" not in payload


@pytest.mark.parametrize("content", ["not json", "[]", "{}"])
def test_planner_rejects_invalid_output(content: str):
    with pytest.raises(PlanningError):
        _run(
            Planner(_FakeProvider(content), _model()).create_plan(
                goal="Fix calculation",
                context=PlannerContext(),
            )
        )


def test_planner_normalizes_initial_revision_to_one():
    payload = _valid_plan()
    payload["revision"] = 7

    plan = _run(
        Planner(
            _FakeProvider(json.dumps(payload)),
            _model(),
        ).create_plan(
            goal="Fix calculation",
            context=PlannerContext(),
        )
    )

    assert plan.revision == 1
