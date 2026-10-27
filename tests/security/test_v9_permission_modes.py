from __future__ import annotations

import json

import pytest

from ai.messages import ToolCall
from agent.approvals import ApprovalDecision
from agent.permissions import PermissionDecisionType
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from agent.tools import AgentTool, AgentToolResult
from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.auto_classifier import SafetyConstraints
from coding_agent.safety.settings import (
    PermissionMode,
    PolicyEffect,
    PolicyRule,
    SecuritySettings,
    load_security_settings,
)


class _Tool(AgentTool):
    description = "test"
    parameters = {"type": "object"}

    def __init__(self, name: str, capabilities: set[str], workspace: Workspace):
        self.name = name
        self.capabilities = frozenset(capabilities)
        self.workspace = workspace

    async def execute(self, tool_call_id, arguments):
        return AgentToolResult(content="ok")


@pytest.mark.parametrize(
    ("mode", "capabilities", "expected"),
    [
        (PermissionMode.DEFAULT, {"create_file", "write_fs"}, PermissionDecisionType.ASK),
        (PermissionMode.DEFAULT, {"edit_file", "write_fs"}, PermissionDecisionType.ASK),
        (PermissionMode.ACCEPT_EDITS, {"create_file", "write_fs"}, PermissionDecisionType.ALLOW),
        (PermissionMode.ACCEPT_EDITS, {"edit_file", "write_fs"}, PermissionDecisionType.ALLOW),
        (PermissionMode.PLAN, {"edit_file", "write_fs"}, PermissionDecisionType.DENY),
        (PermissionMode.DONT_ASK, {"create_file", "write_fs"}, PermissionDecisionType.DENY),
    ],
)
async def test_permission_modes(mode, capabilities, expected, tmp_path):
    tool = _Tool("write", capabilities, Workspace(tmp_path))
    decision = await CodingPermissionEngine(mode=mode).evaluate(
        tool, {"path": "safe.txt"},
    )
    assert decision.decision == expected


async def test_default_allows_safe_shell_and_asks_unknown(tmp_path):
    tool = _Tool("powershell", {"execute"}, Workspace(tmp_path))
    engine = CodingPermissionEngine()
    assert (await engine.evaluate(tool, {"command": "Get-Content app.py"})).decision == PermissionDecisionType.ALLOW
    assert (await engine.evaluate(tool, {"command": "custom-tool --flag"})).decision == PermissionDecisionType.ASK


async def test_read_capability_cannot_shadow_execute_capability(tmp_path):
    tool = _Tool("mixed", {"read_fs", "execute"}, Workspace(tmp_path))
    decision = await CodingPermissionEngine().evaluate(
        tool, {"command": "custom-tool --flag"},
    )
    assert decision.decision == PermissionDecisionType.ASK


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("powershell", "Get-Content C:\\Windows\\win.ini"),
        ("bash", "cat /etc/passwd"),
    ],
)
async def test_read_only_shell_cannot_escape_workspace(name, command, tmp_path):
    decision = await CodingPermissionEngine().evaluate(
        _Tool(name, {"execute"}, Workspace(tmp_path)), {"command": command},
    )
    assert decision.decision == PermissionDecisionType.DENY
    assert decision.rule_id == "workspace_boundary_deny"
    assert decision.hard_deny is True


async def test_network_url_is_ask_not_misclassified_as_a_path(tmp_path):
    decision = await CodingPermissionEngine().evaluate(
        _Tool("powershell", {"execute"}, Workspace(tmp_path)),
        {"command": "Invoke-WebRequest https://example.com"},
    )
    assert decision.decision == PermissionDecisionType.ASK
    assert decision.risk == "network"


async def test_redirection_outside_workspace_is_hard_denied(tmp_path):
    outside = (tmp_path.parent / "outside.txt").resolve()
    decision = await CodingPermissionEngine().evaluate(
        _Tool("powershell", {"execute"}, Workspace(tmp_path)),
        {"command": f"Write-Output unsafe > '{outside}'"},
    )
    assert decision.decision == PermissionDecisionType.DENY
    assert decision.rule_id == "workspace_boundary_deny"


async def test_exact_configured_rule_can_allow_unknown_command(tmp_path):
    settings = SecuritySettings(rules=[PolicyRule(
        id="exact", effect=PolicyEffect.ALLOW, tool="powershell",
        command=["custom-tool", "--safe"],
    )])
    decision = await CodingPermissionEngine(settings=settings).evaluate(
        _Tool("powershell", {"execute"}, Workspace(tmp_path)),
        {"command": "Custom-Tool --safe"},
    )
    assert decision.decision == PermissionDecisionType.ALLOW
    assert decision.rule_id == "exact"


async def test_configured_deny_beats_allow_regardless_of_order(tmp_path):
    settings = SecuritySettings(rules=[
        PolicyRule(id="allow", effect=PolicyEffect.ALLOW, tool="powershell", command=["custom-tool", "*"]),
        PolicyRule(id="deny", effect=PolicyEffect.DENY, tool="powershell", command=["custom-tool", "--danger"]),
    ])
    decision = await CodingPermissionEngine(settings=settings).evaluate(
        _Tool("powershell", {"execute"}, Workspace(tmp_path)),
        {"command": "custom-tool --danger"},
    )
    assert decision.decision == PermissionDecisionType.DENY
    assert decision.rule_id == "deny"


def test_layered_settings_merge_and_conflicting_ids_fail(tmp_path):
    user = tmp_path / "user.json"
    user.write_text(json.dumps({
        "permission_mode": "dont_ask",
        "rules": [{"id": "user-deny", "effect": "deny", "tool": "bash"}],
    }), encoding="utf-8")
    project = tmp_path / "project"
    (project / ".mini_pi").mkdir(parents=True)
    (project / ".mini_pi" / "settings.json").write_text(json.dumps({
        "permission_mode": "default",
        "rules": [{"id": "project-allow", "effect": "allow", "tool": "read"}],
    }), encoding="utf-8")
    settings = load_security_settings(
        project, user_file=user,
        session_override={"permission_mode": "plan"},
    )
    assert settings.permission_mode == PermissionMode.PLAN
    assert {rule.id for rule in settings.rules} == {"user-deny", "project-allow"}

    (project / ".mini_pi" / "settings.json").write_text(json.dumps({
        "rules": [{"id": "user-deny", "effect": "allow", "tool": "bash"}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting duplicate"):
        load_security_settings(project, user_file=user)


def test_command_pattern_only_accepts_terminal_wildcard():
    with pytest.raises(ValueError, match="terminal"):
        PolicyRule(
            id="bad", effect=PolicyEffect.ALLOW,
            tool="bash", command=["git", "*", "status"],
        )


def test_settings_reject_unknown_fields_instead_of_silently_weakening_policy():
    with pytest.raises(ValueError):
        SecuritySettings.model_validate({"permission_mod": "plan"})


class _SessionApproval:
    def __init__(self):
        self.calls = 0

    async def request(self, request):
        self.calls += 1
        return ApprovalDecision.APPROVE_SESSION


async def test_session_approval_is_exact_and_reused(tmp_path):
    workspace = Workspace(tmp_path)
    tool = _Tool("powershell", {"execute"}, workspace)
    engine = CodingPermissionEngine()
    approvals = _SessionApproval()
    dispatcher = ToolDispatcher(
        ToolRegistry([tool]), engine, approval_handler=approvals,
    )
    call = ToolCall(
        id="one", name="powershell", arguments={"command": "custom-tool --safe"},
    )
    assert (await dispatcher.dispatch(call)).is_error is False
    call.id = "two"
    assert (await dispatcher.dispatch(call)).is_error is False
    assert approvals.calls == 1

    different = ToolCall(
        id="three", name="powershell", arguments={"command": "custom-tool --other"},
    )
    assert (await dispatcher.dispatch(different)).is_error is False
    assert approvals.calls == 2


async def test_non_shell_session_approval_is_workspace_and_target_scoped(tmp_path):
    workspace = Workspace(tmp_path)
    tool = _Tool("write", {"create_file", "write_fs"}, workspace)
    engine = CodingPermissionEngine()
    approvals = _SessionApproval()
    dispatcher = ToolDispatcher(
        ToolRegistry([tool]), engine, approval_handler=approvals,
    )
    first = ToolCall(id="one", name="write", arguments={"path": "a.txt"})
    second = ToolCall(id="two", name="write", arguments={"path": "a.txt"})
    other = ToolCall(id="three", name="write", arguments={"path": "b.txt"})
    assert (await dispatcher.dispatch(first)).is_error is False
    assert (await dispatcher.dispatch(second)).is_error is False
    assert approvals.calls == 1
    assert (await dispatcher.dispatch(other)).is_error is False
    assert approvals.calls == 2


async def test_session_approval_cannot_change_capability_on_same_target(tmp_path):
    workspace = Workspace(tmp_path)
    engine = CodingPermissionEngine()
    create = _Tool("workspace_mutation", {"write_fs", "create_file"}, workspace)
    create_decision = await engine.evaluate(create, {"path": "same.txt"})
    engine.remember_approval(create, {"path": "same.txt"}, create_decision)

    delete = _Tool("workspace_mutation", {"write_fs", "delete_file"}, workspace)
    delete_decision = await engine.evaluate(delete, {"path": "same.txt"})
    assert delete_decision.decision == PermissionDecisionType.ASK
    assert delete_decision.rule_id == "delete_file_ask"


async def test_persistent_delete_constraint_cannot_be_overridden_by_allow_rule(tmp_path):
    settings = SecuritySettings(rules=[PolicyRule(
        id="allow-delete", effect=PolicyEffect.ALLOW, tool="delete",
    )])
    engine = CodingPermissionEngine(
        settings=settings,
        safety_constraints=SafetyConstraints(delete_forbidden=True),
    )
    decision = await engine.evaluate(
        _Tool("delete", {"delete_file", "write_fs"}, Workspace(tmp_path)),
        {"path": "victim.txt"},
    )
    assert decision.decision == PermissionDecisionType.DENY
    assert decision.rule_id == "user_delete_constraint_deny"
    assert decision.hard_deny is True


async def test_dynamic_network_argument_still_honors_persistent_constraint(tmp_path):
    engine = CodingPermissionEngine(
        safety_constraints=SafetyConstraints(network_forbidden=True),
    )
    decision = await engine.evaluate(
        _Tool("powershell", {"execute"}, Workspace(tmp_path)),
        {"command": "Invoke-WebRequest $url"},
    )
    assert decision.decision == PermissionDecisionType.DENY
    assert decision.rule_id == "user_safety_constraint_deny"


async def test_push_constraint_handles_git_global_options(tmp_path):
    engine = CodingPermissionEngine(
        safety_constraints=SafetyConstraints(push_forbidden=True),
    )
    decision = await engine.evaluate(
        _Tool("powershell", {"execute"}, Workspace(tmp_path)),
        {"command": "git -C . push origin main"},
    )
    assert decision.decision == PermissionDecisionType.DENY
    assert decision.rule_id == "user_safety_constraint_deny"
