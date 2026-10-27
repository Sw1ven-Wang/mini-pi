"""V7 Stop Rule D: deterministic read-only Viewer."""

from pathlib import Path

import pytest

from coding_agent.agents.models import PlanStep, TaskPlan
from coding_agent.agents.viewer import Viewer, ViewerLimits
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import ObservationSource, ObservationStore


def _plan(*, targets=None, queries=None) -> TaskPlan:
    return TaskPlan(
        objective="Update addition behavior",
        steps=[
            PlanStep(
                id="inspect",
                objective="Locate relevant implementation",
                target_files=list(targets or []),
                evidence_queries=list(queries or []),
                acceptance_criteria=["Relevant code is found"],
            )
        ],
        acceptance_criteria=["Tests pass"],
    )


@pytest.fixture
def repository(tmp_path: Path) -> Workspace:
    src = tmp_path / "src"
    src.mkdir()
    (src / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (src / "unrelated.py").write_text(
        "def greet(name):\n    return f'hello {name}'\n",
        encoding="utf-8",
    )
    return Workspace(tmp_path)


@pytest.mark.asyncio
async def test_viewer_uses_only_read_tools(repository: Workspace):
    before = {
        path.relative_to(repository.root): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }

    viewer = Viewer(repository)
    await viewer.gather(_plan(targets=["src/calc.py"], queries=["def add"]))

    after = {
        path.relative_to(repository.root): path.read_bytes()
        for path in repository.root.rglob("*")
        if path.is_file()
    }
    assert viewer.ALLOWED_OPERATIONS == {"glob", "search", "read"}
    assert after == before


@pytest.mark.asyncio
async def test_viewer_evidence_contains_versions(repository: Workspace):
    store = ObservationStore()
    bundle = await Viewer(repository, observation_store=store).gather(
        _plan(targets=["src/calc.py"])
    )

    snippet = bundle.snippets[0]
    observed = store.get("src/calc.py")
    assert snippet.version.digest
    assert observed is not None
    assert observed.version == snippet.version
    assert observed.source == ObservationSource.READ


@pytest.mark.asyncio
async def test_viewer_paths_are_workspace_relative(repository: Workspace):
    bundle = await Viewer(repository).gather(
        _plan(targets=[r"src\calc.py"], queries=["def add"])
    )

    assert {snippet.path for snippet in bundle.snippets} == {"src/calc.py"}
    assert all(not Path(item.path).is_absolute() for item in bundle.snippets)


@pytest.mark.asyncio
async def test_viewer_respects_evidence_limit(repository: Workspace):
    viewer = Viewer(
        repository,
        limits=ViewerLimits(max_files=1, max_snippets=1, max_total_chars=12),
    )
    bundle = await viewer.gather(_plan(targets=["src/*.py"]))

    assert len(bundle.snippets) <= 1
    assert sum(len(item.content) for item in bundle.snippets) <= 12
    assert bundle.unresolved_questions


@pytest.mark.asyncio
async def test_viewer_excludes_irrelevant_files(repository: Workspace):
    bundle = await Viewer(repository).gather(_plan(queries=["def add"]))

    assert {snippet.path for snippet in bundle.snippets} == {"src/calc.py"}
    assert all("greet" not in snippet.content for snippet in bundle.snippets)


@pytest.mark.asyncio
async def test_viewer_reports_unresolved_questions(repository: Workspace):
    bundle = await Viewer(repository).gather(
        _plan(targets=["src/missing.py"], queries=["does_not_exist"])
    )

    assert bundle.snippets == []
    assert any("not found" in item for item in bundle.unresolved_questions)
    assert any("no matches" in item for item in bundle.unresolved_questions)
