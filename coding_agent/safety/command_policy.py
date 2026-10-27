"""Compatibility policy facade over AST/IR analysis; still not a sandbox."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from coding_agent.safety.command_analysis import (
    CommandAnalysis,
    CommandRisk,
    analyze_command,
)
from coding_agent.safety.shell_ir import ShellKind


class CommandDecisionType(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class CommandPolicyDecision(BaseModel):
    decision: CommandDecisionType
    risk: CommandRisk
    rule_id: str
    reason: str
    suggestion: str | None = None
    analysis: CommandAnalysis


class CommandPolicy:
    def __init__(
        self,
        *,
        allow_build_test: bool = True,
        shell: ShellKind | str = ShellKind.POWERSHELL,
    ) -> None:
        self.allow_build_test = allow_build_test
        self.shell = ShellKind(shell)

    def evaluate(
        self,
        command: str,
        *,
        shell: ShellKind | str | None = None,
    ) -> CommandPolicyDecision:
        analysis = analyze_command(command, shell=shell or self.shell)
        risk = analysis.risk
        if risk == CommandRisk.READ_ONLY:
            return self._decision(analysis, CommandDecisionType.ALLOW, "readonly_allow", "只读命令允许")
        if risk == CommandRisk.BUILD_TEST:
            decision = CommandDecisionType.ALLOW if self.allow_build_test else CommandDecisionType.ASK
            return self._decision(analysis, decision, "build_test_config", "构建/测试命令按 Host 配置处理")
        if risk == CommandRisk.DESTRUCTIVE:
            return self._decision(
                analysis,
                CommandDecisionType.DENY,
                "destructive_shell_deny",
                "破坏性 shell 命令不能绕过专用 mutation 工具",
                _destructive_suggestion(analysis),
            )

        suggestion = None
        if risk == CommandRisk.WRITE:
            suggestion = "Use WriteTool or EditTool for workspace file changes."
        return self._decision(
            analysis,
            CommandDecisionType.ASK,
            f"{risk.value}_ask",
            f"{risk.value} 命令需要显式批准",
            suggestion,
        )

    @staticmethod
    def _decision(analysis, decision, rule_id, reason, suggestion=None):
        return CommandPolicyDecision(
            decision=decision,
            risk=analysis.risk,
            rule_id=rule_id,
            reason=reason,
            suggestion=suggestion,
            analysis=analysis,
        )


def _destructive_suggestion(analysis: CommandAnalysis) -> str:
    rules = set(analysis.matched_rules)
    if "remove-item" in rules or "destructive:remove-item" in rules:
        return "Use DeleteTool instead."
    if "move-item" in rules or "destructive:move-item" in rules:
        return "Use MoveTool instead."
    if "git-reset-hard" in rules or "git-clean-force" in rules:
        return "Inspect git status/diff and use explicit safe file tools instead."
    return "Use a specialized, version-checked mutation tool instead."
