"""Unified V9/V10 coding-agent permission engine."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from agent.permissions import PermissionDecision, PermissionDecisionType
from agent.tools import AgentTool
from coding_agent.safety.auto_classifier import (
    AutoClassificationRequest,
    AutoClassifier,
    AutoDecision,
    SafetyConstraints,
    extract_safety_constraints,
)
from coding_agent.safety.command_analysis import CommandAnalysis, CommandRisk
from coding_agent.safety.command_policy import CommandPolicy
from coding_agent.safety.settings import (
    PermissionMode,
    PolicyEffect,
    PolicyRule,
    SecuritySettings,
    load_security_settings,
)
from coding_agent.safety.shell_ir import ShellKind
from coding_agent.sandbox.base import SandboxLevel
from coding_agent.observability.redaction import redact, redact_text


_EFFECT_PRECEDENCE = {
    PolicyEffect.ALLOW: 1,
    PolicyEffect.ASK: 2,
    PolicyEffect.DENY: 3,
}


class CodingPermissionEngine:
    """One decision point for capabilities, parsed shell operations, and AUTO."""

    def __init__(
        self,
        *,
        settings: SecuritySettings | None = None,
        mode: PermissionMode | str | None = None,
        auto_classifier: AutoClassifier | None = None,
        safety_constraints: SafetyConstraints | None = None,
        user_request: str = "",
    ) -> None:
        self.settings = settings or SecuritySettings()
        self.mode = PermissionMode(mode or self.settings.permission_mode)
        self.auto_classifier = auto_classifier
        self.safety_constraints = safety_constraints or SafetyConstraints()
        self.user_request = user_request
        self.prior_non_readonly: list[dict[str, Any]] = []
        self.git_state: dict[str, Any] = {}
        self.auto_consecutive_blocks = 0
        self.auto_total_blocks = 0
        self._session_grants: set[str] = set()

    @classmethod
    def from_workspace(
        cls,
        workspace: Path,
        *,
        session_override: SecuritySettings | dict[str, Any] | None = None,
        user_file: Path | None = None,
        auto_classifier: AutoClassifier | None = None,
    ) -> "CodingPermissionEngine":
        return cls(
            settings=load_security_settings(
                workspace, session_override=session_override, user_file=user_file,
            ),
            auto_classifier=auto_classifier,
        )

    def update_user_request(self, text: str) -> None:
        self.user_request = text
        self.safety_constraints = extract_safety_constraints(
            text, self.safety_constraints,
        )

    @property
    def auto_active(self) -> bool:
        return (
            self.mode == PermissionMode.AUTO
            and self.settings.auto.enabled
            and self.auto_classifier is not None
        )

    def remember_approval(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
        decision: PermissionDecision,
    ) -> None:
        if decision.hard_deny:
            return
        self._session_grants.add(self._grant_key(tool, arguments, decision))

    def export_session_grants(self) -> list[str]:
        """Opaque exact scope keys, never executable commands or permissive rules."""
        return sorted(self._session_grants)

    def restore_session_grants(self, grants: list[str]) -> None:
        if len(grants) > 1000 or any(not isinstance(key, str) or len(key) > 16_384 for key in grants):
            raise ValueError("invalid session grant ledger")
        self._session_grants = set(grants)

    async def evaluate(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
    ) -> PermissionDecision:
        capabilities = tool.capabilities
        targets, target_error = self._normalized_targets(tool, arguments)
        if target_error is not None:
            return self._decision(
                PermissionDecisionType.DENY,
                "workspace_boundary_deny",
                target_error,
                normalized_targets=targets,
                hard_deny=True,
            )

        read_only = bool(capabilities) and set(capabilities) <= {"read_fs", "search_fs"}
        if self.mode == PermissionMode.PLAN and not read_only:
            return self._decision(
                PermissionDecisionType.DENY,
                "plan_mode_deny",
                "plan 模式禁止 mutation 和 shell 执行",
                normalized_targets=targets,
            )

        if "execute" in capabilities:
            decision = await self._evaluate_shell(tool, arguments, targets)
        else:
            decision = self._evaluate_capabilities(tool, arguments, targets)

        if (
            decision.decision == PermissionDecisionType.ASK
            and self._grant_key(tool, arguments, decision) in self._session_grants
        ):
            decision = decision.model_copy(update={
                "decision": PermissionDecisionType.ALLOW,
                "rule_id": "session_approval_allow",
                "matched_rule_ids": [
                    *decision.matched_rule_ids, "session_approval_allow",
                ],
                "reason": "当前 workspace 内完全相同的操作已获本会话批准",
            })

        if (
            self.mode == PermissionMode.AUTO
            and decision.decision != PermissionDecisionType.ASK
            and decision.rule_id != "auto_classifier_block"
        ):
            self.auto_consecutive_blocks = 0

        if (
            decision.decision == PermissionDecisionType.ASK
            and self.mode == PermissionMode.DONT_ASK
        ):
            decision = decision.model_copy(update={
                "decision": PermissionDecisionType.DENY,
                "rule_id": "dont_ask_deny",
                "matched_rule_ids": [*decision.matched_rule_ids, "dont_ask_deny"],
                "reason": f"dont_ask 模式拒绝待审批操作: {decision.reason}",
            })

        if (
            decision.decision == PermissionDecisionType.ASK
            and self.mode == PermissionMode.AUTO
        ):
            decision = await self._evaluate_auto(tool, decision)

        if not (capabilities and set(capabilities) <= {"read_fs", "search_fs"}):
            self.prior_non_readonly.append({
                "tool_name": tool.name,
                "risk": decision.risk,
                "targets": decision.normalized_targets,
                "decision": decision.decision.value,
            })
            self.prior_non_readonly = self.prior_non_readonly[-20:]
        return decision

    async def _evaluate_shell(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
        targets: list[str],
    ) -> PermissionDecision:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return self._decision(
                PermissionDecisionType.DENY,
                "invalid_command_deny",
                "command 不能为空",
                normalized_targets=targets,
                hard_deny=True,
            )
        shell = ShellKind.BASH if tool.name.lower() == "bash" else ShellKind.POWERSHELL
        policy = CommandPolicy(
            allow_build_test=self.settings.allow_build_test,
            shell=shell,
        )
        policy_decision = policy.evaluate(command)
        analysis = policy_decision.analysis
        parse_result = analysis.parse_result
        shell_paths, shell_path_error = self._shell_paths(tool, analysis)
        if shell_path_error is not None:
            return self._shell_decision(
                PermissionDecisionType.DENY,
                "workspace_boundary_deny",
                shell_path_error,
                analysis,
                list(dict.fromkeys([*targets, *shell_paths])),
                matched_rule_ids=["workspace_boundary_deny"],
                hard_deny=True,
            )
        all_targets = list(dict.fromkeys([*targets, *shell_paths]))

        if any(item.hard_deny for item in analysis.operations):
            return self._shell_decision(
                PermissionDecisionType.DENY,
                policy_decision.rule_id,
                policy_decision.reason,
                analysis,
                all_targets,
                hard_deny=True,
            )
        if self._constraint_blocks(analysis):
            return self._shell_decision(
                PermissionDecisionType.DENY,
                "user_safety_constraint_deny",
                "操作违反持久化的用户安全约束",
                analysis,
                all_targets,
                hard_deny=True,
            )

        configured_effects: list[tuple[PolicyEffect, PolicyRule]] = []
        per_operation_effects: list[PolicyEffect] = []
        per_operation_rules: list[str] = []
        grant_key = self._grant_key_from_hash(
            tool, parse_result.command_hash if parse_result else "",
        )
        has_session_grant = grant_key in self._session_grants

        for operation in analysis.operations:
            matches = [
                rule for rule in self.settings.rules
                if self._rule_matches(
                    rule, tool.name, shell, operation.executable,
                    operation.argv, operation.target, all_targets,
                )
                and not (
                    self.auto_active
                    and rule.broad_shell_allow
                    and operation.risk not in {
                        CommandRisk.READ_ONLY, CommandRisk.BUILD_TEST,
                    }
                )
            ]
            configured_effects.extend((rule.effect, rule) for rule in matches)
            if matches:
                winner = max(matches, key=lambda rule: _EFFECT_PRECEDENCE[rule.effect])
                per_operation_effects.append(winner.effect)
                per_operation_rules.append(winner.id)
            elif has_session_grant:
                per_operation_effects.append(PolicyEffect.ALLOW)
                per_operation_rules.append("session_approval_allow")
            else:
                per_operation_effects.append(self._default_effect(operation.risk))
                per_operation_rules.append(operation.rule_id)

        effect = max(per_operation_effects, key=_EFFECT_PRECEDENCE.__getitem__)
        decision_type = PermissionDecisionType(effect.value)
        risk = analysis.risk.value
        reason = (
            policy_decision.reason
            if not configured_effects
            else "命令按合并后的用户、项目和会话规则处理"
        )
        return self._shell_decision(
            decision_type,
            next(
                (rule_id for rule_id, item_effect in zip(per_operation_rules, per_operation_effects)
                 if item_effect == effect),
                policy_decision.rule_id,
            ),
            reason,
            analysis,
            all_targets,
            matched_rule_ids=list(dict.fromkeys(per_operation_rules)),
            requires_strong_sandbox=(
                self.auto_active
                and decision_type == PermissionDecisionType.ASK
                and self.settings.auto.require_l2_for_shell
            ),
        )

    def _evaluate_capabilities(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
        targets: list[str],
    ) -> PermissionDecision:
        capabilities = tool.capabilities
        if capabilities and set(capabilities) <= {"read_fs", "search_fs"}:
            base = (
                PermissionDecisionType.ALLOW,
                "read_fs_allow" if "read_fs" in capabilities else "search_fs_allow",
                "workspace 内只读操作默认允许",
            )
        elif "delete_file" in capabilities:
            if self.safety_constraints.delete_forbidden:
                return self._decision(
                    PermissionDecisionType.DENY,
                    "user_delete_constraint_deny",
                    "用户明确禁止删除",
                    normalized_targets=targets,
                    matched_rule_ids=["user_delete_constraint_deny"],
                    hard_deny=True,
                )
            base = (PermissionDecisionType.ASK, "delete_file_ask", "删除文件需要用户批准")
        elif "move_file" in capabilities:
            base = (PermissionDecisionType.ASK, "move_file_ask", "移动或重命名文件需要用户批准")
        elif "multi_file_edit" in capabilities:
            base = (
                PermissionDecisionType.ALLOW if self.mode == PermissionMode.ACCEPT_EDITS
                else PermissionDecisionType.ASK,
                "accept_edits_allow" if self.mode == PermissionMode.ACCEPT_EDITS
                else "multi_file_edit_ask",
                "多文件修改按当前权限模式处理",
            )
        elif "edit_file" in capabilities:
            base = (
                PermissionDecisionType.ALLOW if self.mode == PermissionMode.ACCEPT_EDITS
                else PermissionDecisionType.ASK,
                "accept_edits_allow" if self.mode == PermissionMode.ACCEPT_EDITS
                else "edit_file_ask",
                "受版本保护的精确编辑按当前权限模式处理",
            )
        elif "create_file" in capabilities:
            base = (
                PermissionDecisionType.ALLOW if self.mode == PermissionMode.ACCEPT_EDITS
                else PermissionDecisionType.ASK,
                "accept_edits_allow" if self.mode == PermissionMode.ACCEPT_EDITS
                else "create_file_ask",
                "创建文件按当前权限模式处理",
            )
        elif "write_fs" in capabilities:
            base = (PermissionDecisionType.ASK, "write_fs_ask", "文件写入需要用户批准")
        else:
            base = (PermissionDecisionType.DENY, "default_deny", "没有匹配允许规则")

        # Each target needs its own authorization. A docs/* allow cannot
        # authorize a src/* file merely because they share one mutation batch.
        decisions = []
        matched_rule_ids: list[str] = []
        for target in targets or [None]:
            matches = [
                rule for rule in self.settings.rules
                if self._rule_matches(
                    rule, tool.name, None, None, [], None,
                    [target] if target is not None else [],
                )
            ]
            if matches:
                winner = max(matches, key=lambda rule: _EFFECT_PRECEDENCE[rule.effect])
                decisions.append((PermissionDecisionType(winner.effect.value), winner.id, winner.reason))
                matched_rule_ids.extend(rule.id for rule in matches)
            else:
                decisions.append(base)
                matched_rule_ids.append(base[1])
        decision, rule_id, reason = max(
            decisions, key=lambda item: _EFFECT_PRECEDENCE[PolicyEffect(item[0].value)],
        )
        result = self._decision(
            decision, rule_id, reason,
            normalized_targets=targets,
            matched_rule_ids=list(dict.fromkeys(matched_rule_ids)),
            hard_deny=decision == PermissionDecisionType.DENY and (
                rule_id.endswith("_constraint_deny")
            ),
        )
        raw_operations = arguments.get("operations")
        if isinstance(raw_operations, list):
            operations = [
                {"kind": item.get("kind")}
                for item in raw_operations
                if isinstance(item, dict) and isinstance(item.get("kind"), str)
            ][:50]
            destructive = any(
                item["kind"] in {"delete_file", "move_file"}
                for item in operations
            )
            result = result.model_copy(update={
                "operations": operations,
                "risk": "destructive" if destructive else "write",
                "approval_summary": "workspace mutation: " + ", ".join(
                    item["kind"] for item in operations
                ),
            })
        return result

    async def _evaluate_auto(
        self,
        tool: AgentTool,
        decision: PermissionDecision,
    ) -> PermissionDecision:
        if not self.settings.auto.enabled or self.auto_classifier is None:
            return decision.model_copy(update={
                "reason": f"AUTO 分类器未启用，安全降级为 default: {decision.reason}",
            })
        workspace = getattr(tool, "workspace", None)
        if not self.git_state and workspace is not None:
            self.git_state = await _collect_git_state(workspace.root)
        classifier_request = AutoClassificationRequest(
            tool_name=tool.name,
            risk=decision.risk or "unknown",
            operations=decision.operations,
            normalized_targets=decision.normalized_targets,
            user_request=self.user_request,
            constraints=self.safety_constraints,
            prior_mutations=self.prior_non_readonly,
            git_state=self.git_state,
        )
        classifier_request = AutoClassificationRequest.model_validate(
            redact(classifier_request.model_dump(mode="python"))
        )
        result = await self.auto_classifier.classify(classifier_request)
        classifier_metadata = {
            "classifier_result": result.decision.value,
            "classifier_stage": result.stage,
            "classifier_latency_ms": result.latency_ms,
            "classifier_input_tokens": result.estimated_input_tokens,
            "classifier_output_tokens": result.estimated_output_tokens,
        }
        if result.decision == AutoDecision.ASK:
            self.auto_consecutive_blocks = 0
            return decision.model_copy(update={
                "reason": f"{decision.reason}; {result.reason}",
                "matched_rule_ids": [*decision.matched_rule_ids, "auto_fallback_ask"],
                "rule_id": "auto_fallback_ask",
                **classifier_metadata,
            })
        if result.decision == AutoDecision.ALLOW:
            sandbox = getattr(tool, "sandbox", None)
            if (
                "execute" in tool.capabilities
                and self.settings.auto.require_l2_for_shell
                and getattr(sandbox, "level", None)
                != SandboxLevel.L2_CONTAINER
            ):
                return decision.model_copy(update={
                    "reason": "AUTO shell 需要可用的 L2 sandbox，已回退人工审批",
                    "rule_id": "auto_requires_l2_ask",
                    "matched_rule_ids": [
                        *decision.matched_rule_ids, "auto_requires_l2_ask",
                    ],
                    "requires_strong_sandbox": True,
                    **classifier_metadata,
                })
            if (
                "execute" in tool.capabilities
                and self.settings.auto.require_l2_for_shell
            ):
                preflight = getattr(sandbox, "preflight", None)
                workspace = getattr(tool, "workspace", None)
                if not callable(preflight) or workspace is None:
                    return decision.model_copy(update={
                        "reason": "L2 sandbox 无法进行可用性预检，已回退人工审批",
                        "rule_id": "auto_l2_unavailable_ask",
                        "matched_rule_ids": [
                            *decision.matched_rule_ids,
                            "auto_l2_unavailable_ask",
                        ],
                        "requires_strong_sandbox": True,
                        **classifier_metadata,
                    })
                try:
                    await preflight(workspace=workspace)
                except Exception:
                    return decision.model_copy(update={
                        "reason": "L2 sandbox 不可用，已回退人工审批",
                        "rule_id": "auto_l2_unavailable_ask",
                        "matched_rule_ids": [
                            *decision.matched_rule_ids,
                            "auto_l2_unavailable_ask",
                        ],
                        "requires_strong_sandbox": True,
                        **classifier_metadata,
                    })
            self.auto_consecutive_blocks = 0
            return decision.model_copy(update={
                "decision": PermissionDecisionType.ALLOW,
                "reason": result.reason,
                "rule_id": "auto_classifier_allow",
                "matched_rule_ids": [
                    *decision.matched_rule_ids, "auto_classifier_allow",
                ],
                **classifier_metadata,
            })

        self.auto_consecutive_blocks += 1
        self.auto_total_blocks += 1
        if (
            self.auto_consecutive_blocks >= self.settings.auto.consecutive_block_limit
            or self.auto_total_blocks >= self.settings.auto.total_block_limit
        ):
            self.mode = PermissionMode.DEFAULT
            return decision.model_copy(update={
                "reason": "AUTO 阻止次数达到阈值，已降级 default 并请求人工审批",
                "rule_id": "auto_degraded_to_default",
                "matched_rule_ids": [
                    *decision.matched_rule_ids, "auto_degraded_to_default",
                ],
                **classifier_metadata,
            })
        return decision.model_copy(update={
            "decision": PermissionDecisionType.DENY,
            "reason": result.reason,
            "rule_id": "auto_classifier_block",
            "matched_rule_ids": [
                *decision.matched_rule_ids, "auto_classifier_block",
            ],
            **classifier_metadata,
        })

    def _constraint_blocks(self, analysis: CommandAnalysis) -> bool:
        if self.safety_constraints.network_forbidden and any(
            item.risk in {CommandRisk.NETWORK, CommandRisk.INSTALL}
            for item in analysis.operations
        ):
            return True
        if self.safety_constraints.delete_forbidden and any(
            item.risk == CommandRisk.DESTRUCTIVE for item in analysis.operations
        ):
            return True
        if self.safety_constraints.push_forbidden and any(
            item.executable == "git"
            and (
                item.rule_id == "git-network:push"
                or [argument.lower() for argument in item.argv[:1]] == ["push"]
            )
            for item in analysis.operations
        ):
            return True
        return False

    @staticmethod
    def _default_effect(risk: CommandRisk) -> PolicyEffect:
        if risk in {CommandRisk.READ_ONLY, CommandRisk.BUILD_TEST}:
            return PolicyEffect.ALLOW
        if risk == CommandRisk.DESTRUCTIVE:
            return PolicyEffect.DENY
        return PolicyEffect.ASK

    @staticmethod
    def _rule_matches(
        rule: PolicyRule,
        tool_name: str,
        shell: ShellKind | None,
        executable: str | None,
        argv: list[str],
        operation_target: str | None,
        targets: list[str],
    ) -> bool:
        if rule.tool not in {"*", tool_name}:
            return False
        if rule.shell is not None and (
            shell is None or rule.shell != shell.value
        ):
            return False
        if rule.command is not None:
            actual = [executable or "", *argv]
            pattern = rule.command
            if shell == ShellKind.POWERSHELL:
                actual = [item.lower() for item in actual]
                pattern = [item.lower() for item in pattern]
            if pattern[-1:] == ["*"]:
                if actual[: len(pattern) - 1] != pattern[:-1]:
                    return False
            elif actual != pattern:
                return False
        if rule.paths:
            candidates = [item for item in [operation_target, *targets] if item]
            if not any(
                fnmatch.fnmatchcase(candidate.replace("\\", "/"), pattern)
                for candidate in candidates
                for pattern in rule.paths
            ):
                return False
        return True

    @staticmethod
    def _shell_paths(
        tool: AgentTool,
        analysis: CommandAnalysis,
    ) -> tuple[list[str], str | None]:
        workspace = getattr(tool, "workspace", None)
        if workspace is None:
            return [], None
        path_consumers = {
            "get-content", "get-childitem", "test-path", "select-string",
            "cat", "head", "tail", "ls", "dir", "wc",
            "set-content", "add-content", "out-file", "copy-item", "new-item",
            "rename-item", "set-item", "clear-content", "tee", "touch",
            "mkdir", "cp", "sed",
            "remove-item", "move-item", "rename-item", "rm", "rmdir",
            "mv", "del", "erase", "shred", "unlink", "rename",
            "pytest", "ruff", "python", "python3", "uv", "dotnet",
            "cargo", "go",
        }
        normalized: list[str] = []
        for operation in analysis.operations:
            if operation.target is not None:
                try:
                    resolved = _resolve_shell_path(workspace, operation.target)
                    normalized.append(
                        resolved.relative_to(workspace.root).as_posix() or "."
                    )
                except Exception:
                    return normalized, (
                        "shell 重定向尝试写入 workspace 之外: "
                        f"{operation.target}"
                    )
            if operation.executable is None:
                continue
            relative_candidates = _path_arguments(
                operation.executable, operation.argv,
            )
            candidates: list[str] = []
            for argument in operation.argv:
                if not argument:
                    continue
                candidate_argument = argument
                if argument.startswith("-"):
                    if "=" not in argument:
                        continue
                    candidate_argument = argument.split("=", 1)[1]
                    if not candidate_argument:
                        continue
                is_uri = bool(re.match(
                    r"^[A-Za-z][A-Za-z0-9+.-]*://", candidate_argument,
                ))
                if is_uri and operation.risk == CommandRisk.NETWORK:
                    continue
                explicit = bool(
                    re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]{1,2}|~[\\/]|\.\.[\\/])", candidate_argument)
                    or "/../" in candidate_argument.replace("\\", "/")
                    or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate_argument)
                )
                known_path = (
                    operation.executable in path_consumers
                    and argument in relative_candidates
                    and not re.search(r"[*?\[\]]", candidate_argument)
                )
                relative_pathish = (
                    not candidate_argument.startswith("-")
                    and not re.search(r"[*?\[\]]", candidate_argument)
                    and (
                        "/" in candidate_argument
                        or "\\" in candidate_argument
                        or (workspace.root / candidate_argument).exists()
                        or (workspace.root / candidate_argument).is_symlink()
                    )
                )
                if explicit or known_path or relative_pathish:
                    candidates.append(candidate_argument)
            for candidate in candidates:
                try:
                    resolved = _resolve_shell_path(workspace, candidate)
                    normalized.append(
                        resolved.relative_to(workspace.root).as_posix() or "."
                    )
                except Exception:
                    return normalized, (
                        "shell 命令尝试访问 workspace 之外的路径或 provider: "
                        f"{candidate}"
                    )
        return list(dict.fromkeys(normalized)), None

    @staticmethod
    def _normalized_targets(
        tool: AgentTool,
        arguments: dict[str, Any],
    ) -> tuple[list[str], str | None]:
        raw: list[str] = []
        for key in ("path", "source", "destination", "cwd"):
            value = arguments.get(key)
            if isinstance(value, str):
                raw.append(value)
        files = arguments.get("files")
        if isinstance(files, list):
            raw.extend(
                str(item["path"]) for item in files
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
        workspace = getattr(tool, "workspace", None)
        if workspace is None:
            return list(dict.fromkeys(raw)), None
        normalized: list[str] = []
        for value in raw:
            try:
                resolved = workspace.resolve(value)
                normalized.append(resolved.relative_to(workspace.root).as_posix() or ".")
            except Exception:
                return normalized, f"路径位于 workspace 之外或无法规范化: {value}"
        return list(dict.fromkeys(normalized)), None

    @classmethod
    def _grant_key_from_hash(cls, tool: AgentTool, command_hash: str) -> str:
        return f"{cls._workspace_scope(tool)}:{tool.name}:command:{command_hash}"

    def _grant_key(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
        decision: PermissionDecision,
    ) -> str:
        if decision.command_hash:
            return self._grant_key_from_hash(tool, decision.command_hash)
        targets = ",".join(sorted(decision.normalized_targets))
        capabilities = ",".join(sorted(tool.capabilities))
        return (
            f"{self._workspace_scope(tool)}:{tool.name}:"
            f"capabilities:{capabilities}:targets:{targets}"
        )

    @staticmethod
    def _workspace_scope(tool: AgentTool) -> str:
        workspace = getattr(tool, "workspace", None)
        root = getattr(workspace, "root", None)
        if root is None:
            return "workspace:none"
        normalized = os.path.normcase(str(Path(root).resolve(strict=False)))
        return "workspace:" + hashlib.sha256(
            normalized.encode("utf-8", "surrogatepass")
        ).hexdigest()

    @staticmethod
    def _decision(
        decision: PermissionDecisionType,
        rule_id: str,
        reason: str,
        *,
        normalized_targets: list[str] | None = None,
        matched_rule_ids: list[str] | None = None,
        hard_deny: bool = False,
    ) -> PermissionDecision:
        targets = normalized_targets or []
        return PermissionDecision(
            decision=decision,
            rule_id=rule_id,
            reason=reason,
            normalized_target=targets[0] if len(targets) == 1 else None,
            normalized_targets=targets,
            matched_rule_ids=matched_rule_ids or [rule_id],
            hard_deny=hard_deny,
        )

    @staticmethod
    def _shell_decision(
        decision: PermissionDecisionType,
        rule_id: str,
        reason: str,
        analysis: CommandAnalysis,
        targets: list[str],
        *,
        matched_rule_ids: list[str] | None = None,
        hard_deny: bool = False,
        requires_strong_sandbox: bool = False,
    ) -> PermissionDecision:
        parse_result = analysis.parse_result
        operation_summaries = [
            {
                **item.model_dump(mode="json", exclude={"argv", "target"}),
                "argv": _safe_argv(item.executable, item.argv),
            }
            for item in analysis.operations
        ]
        summary_parts = [
            f"{item.risk.value}: "
            + " ".join([
                item.executable or item.rule_id,
                *_safe_argv(item.executable, item.argv),
            ]).strip()
            for item in analysis.operations
        ]
        return PermissionDecision(
            decision=decision,
            rule_id=rule_id,
            reason=reason,
            matched_rule_ids=matched_rule_ids or analysis.matched_rules or [rule_id],
            operations=operation_summaries,
            risk=analysis.risk.value,
            normalized_target=targets[0] if len(targets) == 1 else None,
            normalized_targets=targets,
            parser_status=(parse_result.status.value if parse_result else None),
            approval_summary="; ".join(summary_parts),
            hard_deny=hard_deny,
            command_hash=(parse_result.command_hash if parse_result else None),
            requires_strong_sandbox=requires_strong_sandbox,
        )


async def _collect_git_state(workspace: Path) -> dict[str, Any]:
    """Collect bounded git metadata without file contents, hooks, or network."""
    if not (workspace / ".git").exists():
        return {"repository": False}
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() in {
            "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
        }
    }
    try:
        branch_process = await asyncio.create_subprocess_exec(
            "git", "-c", "core.fsmonitor=false", "symbolic-ref", "--quiet",
            "--short", "HEAD",
            cwd=str(workspace), env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        branch_stdout, _ = await asyncio.wait_for(
            branch_process.communicate(), timeout=2,
        )
        dirty_process = await asyncio.create_subprocess_exec(
            "git", "-c", "core.fsmonitor=false", "diff-index", "--quiet",
            "HEAD", "--",
            cwd=str(workspace), env=environment,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(dirty_process.wait(), timeout=2)
        branch = branch_stdout[:512].decode("utf-8", "replace").strip()
        return {
            "repository": True,
            "branch": branch or "detached",
            "tracked_changes": dirty_process.returncode == 1,
        }
    except Exception:
        return {"repository": True, "state": "unavailable"}


def _path_arguments(executable: str, argv: list[str]) -> list[str]:
    path_flags = {
        "-path", "-literalpath", "-destination", "-target", "-filepath",
        "-source", "-newname",
    }
    value_flags = {
        "-pattern", "-value", "-filter", "-include", "-exclude",
        "-encoding", "-type", "-name", "-itemtype",
        "-n", "--lines", "-c", "--bytes", "--max-count",
    }
    flagged: list[str] = []
    positional: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        lowered = item.lower()
        if lowered in path_flags and index + 1 < len(argv):
            flagged.append(argv[index + 1])
            index += 2
            continue
        if lowered in value_flags and index + 1 < len(argv):
            index += 2
            continue
        if item and not item.startswith("-"):
            positional.append(item)
        index += 1
    limits = {
        "set-content": 1, "add-content": 1, "out-file": 1,
        "new-item": 1, "set-item": 1, "clear-content": 1,
        "rename-item": 2, "copy-item": 2, "cp": 2,
    }
    limit = limits.get(executable)
    selected = positional if limit is None else positional[:limit]
    return list(dict.fromkeys([*flagged, *selected]))


def _resolve_shell_path(workspace, value: str) -> Path:
    """Normalize shell paths while permitting absolute paths only in-root."""
    if not value or "\x00" in value:
        raise ValueError("invalid shell path")
    native = Path(value).expanduser()
    windows = PureWindowsPath(value)
    if native.is_absolute():
        resolved = native.resolve(strict=False)
        resolved.relative_to(workspace.root)
        return resolved
    if windows.is_absolute() or windows.drive:
        # A Windows absolute/provider path cannot be represented safely on a
        # non-Windows host; on Windows native.is_absolute() handled it above.
        raise ValueError("foreign absolute path or PowerShell provider")
    return workspace.resolve(value)


def _safe_argv(executable: str | None, argv: list[str]) -> list[str]:
    """Produce useful IR tokens without retaining payloads, secrets, or host paths."""
    result: list[str] = []
    redact_next = False
    opaque_next = False
    retained_positionals = 0
    for raw in argv[:64]:
        token = redact_text(str(raw)[:512])
        lowered = token.lower()
        if redact_next:
            result.append("<redacted-value>")
            redact_next = False
            continue
        if opaque_next:
            result.append("<opaque-code>")
            opaque_next = False
            continue
        sensitive_flag = lowered in {
            "-value", "--value", "-body", "--body", "-data", "--data",
            "-d", "--header", "-h", "-headers", "--token", "--password",
            "--secret", "--api-key", "--authorization",
        } or any(
            hint in lowered.lstrip("-").replace("_", "-")
            for hint in ("password", "secret", "token", "api-key", "authorization")
        )
        if sensitive_flag:
            if "=" in token:
                result.append(token.split("=", 1)[0] + "=<redacted-value>")
            elif ":" in token:
                result.append(token.split(":", 1)[0] + ":<redacted-value>")
            else:
                result.append(token)
                redact_next = True
            continue
        interpreter = (executable or "").lower() in {
            "python", "python3", "node", "bash", "sh", "zsh",
            "powershell", "pwsh",
        }
        flag_base = lowered.split(":", 1)[0].split("=", 1)[0]
        encoded_flag = (
            len(flag_base) >= len("-enc")
            and flag_base.startswith("-enc")
            and "-encodedcommand".startswith(flag_base)
        )
        if interpreter and (flag_base in {"-c", "-e", "-command"} or encoded_flag):
            if ":" in token or "=" in token:
                result.append(flag_base + "=<opaque-code>")
            else:
                result.append(token)
                opaque_next = True
            continue
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", token):
            parsed = urlsplit(token)
            host = parsed.hostname or "invalid-host"
            port = f":{parsed.port}" if parsed.port is not None else ""
            result.append(f"{parsed.scheme.lower()}://{host.lower()}{port}")
            continue
        if (
            Path(token).is_absolute()
            or PureWindowsPath(token).is_absolute()
            or PureWindowsPath(token).drive
            or token.startswith(("~/", "~\\", "../", "..\\"))
        ):
            result.append("<path>")
            continue
        if "=" in token and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            result.append(token.split("=", 1)[0] + "=<redacted-value>")
            continue
        if token.startswith("-"):
            result.append(token)
            continue
        # Keep at most the first structural positional (typically a git or
        # package-manager subcommand); hide arbitrary payload literals.
        if retained_positionals == 0:
            result.append(token)
        else:
            result.append("<arg>")
        retained_positionals += 1
    return result
