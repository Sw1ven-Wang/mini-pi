"""Security boundaries reported in the workbench code review."""

import pytest

from ai.messages import ToolCall
from agent.permissions import PermissionDecisionType as Decision
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.filesystem.workspace import Workspace
from coding_agent.orchestration import _WorkspaceMutationPermissionTool
from coding_agent.process.runner import ProcessRunner
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import PolicyRule, SecuritySettings
from coding_agent.tools.bash import BashTool
from coding_agent.tools.powershell import PowerShellTool
from coding_agent.tools.read import ReadTool


@pytest.mark.parametrize("tool_class", [BashTool, PowerShellTool])
@pytest.mark.parametrize("command", [
    "uv run --no-sync python payload.py pytest",
    "uv run python payload.py pytest",
    "uv run --with pytest python payload.py",
    "uv run node payload.js pytest",
    "uv run bash -c payload pytest",
    "uv run python -c payload pytest",
    "uv run Python -m Pytest",
    "rg --pre payload.exe pattern README.md",
    "rg --pre=payload.exe pattern README.md",
    "rg --hostname-bin=payload.exe pattern README.md",
    "git diff --output=out.diff",
    "git log --output out.log",
    "git diff --ext-diff",
    "git show --textconv HEAD",
    "git -c core.pager=payload log",
    "git -ccore.pager=payload log",
    "git diff --out=out.diff",
    "git show --textc HEAD",
    "git --paginate log",
])
async def test_executable_and_side_effect_arguments_never_auto_allow(tool_class, command, tmp_path):
    tool = tool_class(Workspace(tmp_path), ProcessRunner())
    result = await CodingPermissionEngine().evaluate(tool, {"command": command})
    assert result.decision != Decision.ALLOW, command


@pytest.mark.parametrize("tool_class", [BashTool, PowerShellTool])
@pytest.mark.parametrize("command", [
    "uv run pytest -q",
    "uv run python -m pytest -q",
    "uv run --no-sync --offline python -m pytest -q",
    "uv run -- pytest -q",
    "rg -n pattern README.md",
    "git diff --stat",
    "git status --short",
    "git -C . status --short",
])
async def test_legitimate_read_and_test_commands_stay_automatic(tool_class, command, tmp_path):
    tool = tool_class(Workspace(tmp_path), ProcessRunner())
    result = await CodingPermissionEngine().evaluate(tool, {"command": command})
    assert result.decision == Decision.ALLOW, command


@pytest.mark.parametrize("mode", ["default", "plan", "accept_edits", "auto", "dont_ask"])
async def test_explicit_read_deny_precedes_default_in_every_mode(mode, tmp_path):
    (tmp_path / ".env").write_text("PRIVATE_SENTINEL=must-not-read", encoding="utf-8")
    tool = ReadTool(Workspace(tmp_path))
    engine = CodingPermissionEngine(settings=SecuritySettings(
        permission_mode=mode,
        rules=[
            PolicyRule(id="read-allow", effect="allow", tool="read"),
            PolicyRule(id="no-secrets", effect="deny", tool="read", paths=[".env"]),
        ],
    ))
    decision = await engine.evaluate(tool, {"path": ".env"})
    assert decision.decision == Decision.DENY
    assert "no-secrets" in decision.matched_rule_ids
    dispatcher = ToolDispatcher(ToolRegistry([tool]), engine)
    result = await dispatcher.dispatch(ToolCall(id="read", name="read", arguments={"path": ".env"}))
    assert result.is_error
    assert "must-not-read" not in result.content
    assert (await engine.evaluate(tool, {"path": "public.txt"})).decision == Decision.ALLOW


@pytest.mark.parametrize(("mode", "expected"), [("default", Decision.ASK), ("dont_ask", Decision.DENY)])
async def test_read_ask_rules_follow_permission_mode(mode, expected, tmp_path):
    engine = CodingPermissionEngine(settings=SecuritySettings(
        permission_mode=mode,
        rules=[PolicyRule(id="ask-read", effect="ask", tool="read", paths=["private.txt"])],
    ))
    assert (await engine.evaluate(ReadTool(Workspace(tmp_path)), {"path": "private.txt"})).decision == expected


@pytest.mark.parametrize(("paths", "extra_rules", "expected"), [
    (["docs/a.md", "src/a.py"], [], Decision.ASK),
    (["docs/a.md", "docs/b.md"], [], Decision.ALLOW),
    (["docs/a.md", "src/a.py"], [PolicyRule(id="src", effect="allow", paths=["src/*"])], Decision.ALLOW),
    (["docs/a.md", "docs/private.md"], [PolicyRule(id="secret", effect="deny", paths=["docs/private.md"])], Decision.DENY),
    (["src/a.py", "docs/a.md"], [], Decision.ASK),
])
async def test_batch_authorizes_every_target_independently(paths, extra_rules, expected, tmp_path):
    workspace = Workspace(tmp_path)
    changes = WorkspaceChangeSet(operations=[CreateFileOp(path=path, content="test") for path in paths])
    tool = _WorkspaceMutationPermissionTool(workspace, changes)
    engine = CodingPermissionEngine(settings=SecuritySettings(rules=[
        PolicyRule(id="docs", effect="allow", paths=["docs/*"]), *extra_rules,
    ]))
    result = await engine.evaluate(tool, {"files": [{"path": path} for path in paths]})
    assert result.decision == expected
    assert set(result.normalized_targets) == set(paths)


async def test_move_requires_both_source_and_destination_authorized(tmp_path):
    from coding_agent.tools.move import MoveTool
    from coding_agent.filesystem.mutation_queue import FileMutationQueue
    from coding_agent.changes.checkpoint import CheckpointStore

    tool = MoveTool(Workspace(tmp_path), FileMutationQueue(), CheckpointStore())
    engine = CodingPermissionEngine(settings=SecuritySettings(rules=[
        PolicyRule(id="docs", effect="allow", paths=["docs/*"]),
    ]))
    result = await engine.evaluate(tool, {"source": "docs/a.md", "destination": "src/a.py"})
    assert result.decision == Decision.ASK


async def test_viewer_and_search_never_expose_denied_evidence(tmp_path):
    from coding_agent.agents.viewer import Viewer
    from coding_agent.agents.models import PlanStep, TaskPlan
    from coding_agent.errors import PermissionDeniedError

    (tmp_path / "private.py").write_text("PRIVATE_SENTINEL=123\n", encoding="utf-8")
    (tmp_path / "public.py").write_text("public=123\n", encoding="utf-8")
    engine = CodingPermissionEngine(settings=SecuritySettings(rules=[
        PolicyRule(id="no-private", effect="deny", tool="read", paths=["private.py"]),
    ]))
    viewer = Viewer(Workspace(tmp_path), permission_engine=engine)
    plan = TaskPlan(objective="Inspect", steps=[PlanStep(id="read", objective="Inspect", target_files=["private.py"])], acceptance_criteria=["safe"])
    with pytest.raises(PermissionDeniedError):
        await viewer.gather(plan)
    searched = plan.model_copy(update={"steps": [PlanStep(id="search", objective="Inspect", evidence_queries=["123"])]})
    evidence = await viewer.gather(searched)
    assert {item.path for item in evidence.search_results} == {"public.py"}
    assert "PRIVATE_SENTINEL" not in evidence.model_dump_json()
