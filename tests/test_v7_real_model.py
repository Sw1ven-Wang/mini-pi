"""V7 Stop Rule M: opt-in smoke against the configured real model."""

import os

import pytest

from ai.models import Model
from ai.providers.openai import OpenAIProvider
from coding_agent.agents.editor import Editor
from coding_agent.agents.models import (
    ChangeIntent,
    EditorRequest,
    EvidenceBundle,
    EvidenceSnippet,
    PlannerContext,
)
from coding_agent.agents.planner import Planner
from coding_agent.filesystem.versions import FileVersion


_API_KEY = os.getenv("DEEPSEEK_API_KEY")
_OPTED_IN = os.getenv("RUN_REAL_MODEL_SMOKE") == "1"
pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        not _API_KEY or not _OPTED_IN,
        reason=(
            "requires DEEPSEEK_API_KEY and RUN_REAL_MODEL_SMOKE=1; "
            "FakeModel results must not be reported as real-model smoke"
        ),
    ),
]


def _runtime():
    provider = OpenAIProvider(
        api_key=_API_KEY,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = Model(
        id=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        provider="deepseek",
    )
    return provider, model


@pytest.mark.parametrize(
    "goal",
    [
        "Fix a one-line arithmetic bug in src/calc.py",
        "Rename an internal function while preserving its compatibility wrapper",
        "Create a small validation helper and its tests",
    ],
)
async def test_real_model_planner_returns_bounded_plan(goal: str):
    provider, model = _runtime()

    plan = await Planner(provider, model).create_plan(
        goal=goal,
        context=PlannerContext(
            constraints=["Use one model and do not mutate while planning"],
            repository_map_summary="src/calc.py | functions=add\ntests/test_calc.py",
        ),
    )

    assert 1 <= len(plan.steps) <= 10
    assert plan.revision == 1
    assert plan.acceptance_criteria


async def test_real_model_editor_returns_version_bound_change_ir():
    provider, model = _runtime()
    request = EditorRequest(
        intent=ChangeIntent(
            objective="Fix add",
            target_files=["src/calc.py"],
            requirements=["Return a + b instead of a - b"],
            invariants=["Keep the signature"],
            acceptance_criteria=["add(2, 3) returns 5"],
        ),
        evidence=EvidenceBundle(
            snippets=[
                EvidenceSnippet(
                    path="src/calc.py",
                    start_line=1,
                    end_line=2,
                    content="def add(a, b):\n    return a - b",
                    version=FileVersion(digest="a" * 64, size_bytes=31),
                )
            ]
        ),
    )

    change_set = await Editor(provider, model).create_change_set(request=request)

    assert len(change_set.operations) == 1
    assert change_set.operations[0].kind == "edit_file"
    assert change_set.operations[0].expected_version.digest == "a" * 64
