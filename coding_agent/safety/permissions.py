"""Coding Agent permission policy."""

from __future__ import annotations

from typing import Any

from agent.permissions import (
    PermissionDecision,
    PermissionDecisionType,
)
from agent.tools import AgentTool


class CodingPermissionEngine:
    """Coding Agent 第一版权限策略。"""

    async def evaluate(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
    ) -> PermissionDecision:

        capabilities = tool.capabilities

        # ------------------------------------------
        # 只读文件系统
        # ------------------------------------------

        if "read_fs" in capabilities:
            return PermissionDecision(
                decision=PermissionDecisionType.ALLOW,
                rule_id="read_fs_allow",
                reason="workspace 内只读操作默认允许",
            )

        # ------------------------------------------
        # Edit
        # ------------------------------------------

        if "edit_file" in capabilities:
            return PermissionDecision(
                decision=PermissionDecisionType.ALLOW,
                rule_id="edit_file_allow",
                reason=(
                    "workspace 内受版本和 mutation "
                    "保护的精确编辑默认允许"
                ),
            )

        # ------------------------------------------
        # 创建文件
        # ------------------------------------------

        if "create_file" in capabilities:
            return PermissionDecision(
                decision=PermissionDecisionType.ASK,
                rule_id="create_file_ask",
                reason="创建新文件需要用户批准",
            )

        # ------------------------------------------
        # 本地shell能力
        # ------------------------------------------
        if "execute" in capabilities:
            return PermissionDecision(
                decision=PermissionDecisionType.ASK,
                rule_id="execute_ask",
                reason="执行本地 shell 命令需要用户批准",
            )

        return PermissionDecision(
            decision=PermissionDecisionType.DENY,
            rule_id="default_deny",
            reason="没有匹配允许规则",
        )