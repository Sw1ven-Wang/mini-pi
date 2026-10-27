"""Tool dispatcher."""

from __future__ import annotations

import json
import logging
import time
import asyncio

from ai.messages import (
    ToolCall,
    ToolResultMessage,
)

from agent.permissions import (
    PermissionDecisionType,
    PermissionEngine,
)

from coding_agent.errors import (
    ApprovalRequiredError,
    CodingAgentError,
    CommandPolicyBlockedError,
    InvalidArgumentError,
    PermissionDeniedError,
)

from .tool_registry import ToolRegistry
from .hooks import ToolBudget, ToolLifecycleObserver, ToolResultObserver
from agent.approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
)

logger = logging.getLogger(__name__)


class ToolDispatcher:
    """统一调度 AgentTool。

    当前执行流程：

    ToolCall
        ↓
    ToolRegistry 查找
        ↓
    PermissionEngine.evaluate()
        ↓
    ALLOW / ASK / DENY
        ↓
    Tool.execute()
        ↓
    AgentToolResult
        ↓
    [result_observer.on_tool_result]   (optional)
        ↓
    ToolResultMessage

    可预期的 CodingAgentError 会被安全序列化返回模型。

    未预期异常不会直接将 traceback、
    本机路径或内部实现细节暴露给模型。
    """

    def __init__(
            self,
            registry: ToolRegistry,
            permission_engine: PermissionEngine,
            approval_handler: ApprovalHandler | None = None,
            result_observer: ToolResultObserver | None = None,
            lifecycle_observer: ToolLifecycleObserver | None = None,
            budget: ToolBudget | None = None,
        ) -> None:

            self._registry = registry
            self._permission_engine = (
                permission_engine
            )

            self._approval_handler = (
                approval_handler
            )

            self._result_observer = result_observer
            self._lifecycle_observer = lifecycle_observer
            self._budget = budget

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def result_observer(self) -> ToolResultObserver | None:
        return self._result_observer

    @property
    def permission_engine(self) -> PermissionEngine:
        return self._permission_engine

    @property
    def approval_handler(self) -> ApprovalHandler | None:
        return self._approval_handler

    def set_result_observer(
        self,
        observer: ToolResultObserver | None,
    ) -> None:
        self._result_observer = observer

    def set_budget(self, budget: ToolBudget | None) -> None:
        self._budget = budget

    async def dispatch(
        self,
        tool_call: ToolCall,
    ) -> ToolResultMessage:

        if self._budget is not None:
            self._budget.consume_tool_call()

        # --------------------------------------------------
        # 1. 查找工具
        # --------------------------------------------------

        tool = self._registry.get(
            tool_call.name
        )

        if tool is None:
            error = InvalidArgumentError(
                f"未知工具: {tool_call.name}"
            )

            return ToolResultMessage(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(
                    error.to_dict(),
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        capabilities = sorted(tool.capabilities)
        await self._notify_lifecycle(
            stage="requested",
            tool_name=tool.name,
            call_id=tool_call.id,
            capabilities=capabilities,
        )
        started_at: float | None = None
        terminal_lifecycle_emitted = False

        try:
            # --------------------------------------------------
            # 2. Permission Check
            # --------------------------------------------------

            decision = (
                await self._permission_engine.evaluate(
                    tool,
                    tool_call.arguments,
                )
            )

            await self._notify_lifecycle(
                stage="policy_evaluated",
                tool_name=tool.name,
                call_id=tool_call.id,
                capabilities=capabilities,
                permission_decision=decision.decision.value,
                rule_id=decision.rule_id,
                matched_rule_ids=decision.matched_rule_ids,
                risk=decision.risk,
                parser_status=decision.parser_status,
                approval_summary=decision.approval_summary,
                command_hash=decision.command_hash,
                reason=decision.reason,
                operations=decision.operations,
                normalized_targets=decision.normalized_targets,
            )
            if decision.rule_id.startswith("auto_"):
                await self._notify_lifecycle(
                    stage="auto_classified",
                    tool_name=tool.name,
                    call_id=tool_call.id,
                    capabilities=capabilities,
                    permission_decision=decision.decision.value,
                    rule_id=decision.rule_id,
                    risk=decision.risk,
                    reason=decision.reason,
                    duration_ms=decision.classifier_latency_ms,
                    classifier_result=decision.classifier_result,
                    classifier_stage=decision.classifier_stage,
                    classifier_input_tokens=decision.classifier_input_tokens,
                    classifier_output_tokens=decision.classifier_output_tokens,
                )

            # --------------------------------------------------
            # 3. DENY
            # --------------------------------------------------

            if (
                decision.decision
                == PermissionDecisionType.DENY
            ):
                await self._notify_lifecycle(
                    stage="denied",
                    tool_name=tool.name,
                    call_id=tool_call.id,
                    capabilities=capabilities,
                    permission_decision=decision.decision.value,
                    rule_id=decision.rule_id,
                    matched_rule_ids=decision.matched_rule_ids,
                    risk=decision.risk,
                    parser_status=decision.parser_status,
                    command_hash=decision.command_hash,
                    reason=decision.reason,
                    error_code=(
                        "command_policy_blocked"
                        if decision.hard_deny and decision.risk == "destructive"
                        else "permission_denied"
                    ),
                )
                terminal_lifecycle_emitted = True
                if decision.hard_deny and decision.risk == "destructive":
                    raise CommandPolicyBlockedError(
                        risk=decision.risk,
                        rule_id=decision.rule_id,
                        reason=decision.reason,
                        suggestion=(
                            "Use DeleteTool instead."
                            if "remove-item" in decision.rule_id
                            else "Use MoveTool instead."
                            if "move-item" in decision.rule_id
                            else "Use a specialized, version-checked mutation tool instead."
                        ),
                    )
                raise PermissionDeniedError(
                    tool=tool.name,
                    reason=decision.reason,
                )

            # --------------------------------------------------
            # 4. ASK
            # --------------------------------------------------

            if (
                decision.decision
                == PermissionDecisionType.ASK
            ):
                await self._notify_lifecycle(
                    stage="approval_requested",
                    tool_name=tool.name,
                    call_id=tool_call.id,
                    capabilities=capabilities,
                    permission_decision=decision.decision.value,
                    rule_id=decision.rule_id,
                    matched_rule_ids=decision.matched_rule_ids,
                    risk=decision.risk,
                    parser_status=decision.parser_status,
                    approval_summary=decision.approval_summary,
                    command_hash=decision.command_hash,
                    reason=decision.reason,
                    operations=decision.operations,
                    normalized_targets=decision.normalized_targets,
                )
                 # 没有交互式审批能力
                if self._approval_handler is None:
                    await self._notify_lifecycle(
                        stage="denied",
                        tool_name=tool.name,
                        call_id=tool_call.id,
                        capabilities=capabilities,
                        permission_decision=decision.decision.value,
                        rule_id=decision.rule_id,
                    )
                    terminal_lifecycle_emitted = True
                    raise ApprovalRequiredError(
                        tool=tool.name,
                        reason=decision.reason,
                    )

                approval = (
                    await self._approval_handler.request(
                        ApprovalRequest(
                            tool_name=tool.name,
                            arguments=tool_call.arguments,
                            reason=decision.reason,
                            rule_id=decision.rule_id,
                            matched_rule_ids=decision.matched_rule_ids,
                            risk=decision.risk,
                            normalized_targets=decision.normalized_targets,
                            summary=decision.approval_summary,
                            command_hash=decision.command_hash,
                        )
                    )
                )

                if (
                    approval
                    == ApprovalDecision.DENY
                ):
                    await self._notify_lifecycle(
                        stage="approval_resolved",
                        tool_name=tool.name,
                        call_id=tool_call.id,
                        capabilities=capabilities,
                        permission_decision="deny",
                        rule_id=decision.rule_id,
                        risk=decision.risk,
                        command_hash=decision.command_hash,
                        approval_scope="once",
                        reason="用户拒绝了该操作",
                    )
                    await self._notify_lifecycle(
                        stage="denied",
                        tool_name=tool.name,
                        call_id=tool_call.id,
                        capabilities=capabilities,
                        permission_decision="deny",
                        rule_id=decision.rule_id,
                    )
                    terminal_lifecycle_emitted = True
                    raise PermissionDeniedError(
                        tool=tool.name,
                        reason="用户拒绝了该操作",
                    )

                if approval == ApprovalDecision.APPROVE_SESSION:
                    remember = getattr(
                        self._permission_engine,
                        "remember_approval",
                        None,
                    )
                    if callable(remember):
                        remember(tool, tool_call.arguments, decision)
                await self._notify_lifecycle(
                    stage="approval_resolved",
                    tool_name=tool.name,
                    call_id=tool_call.id,
                    capabilities=capabilities,
                    permission_decision="allow",
                    rule_id=decision.rule_id,
                    risk=decision.risk,
                    command_hash=decision.command_hash,
                    approval_scope=(
                        "session"
                        if approval == ApprovalDecision.APPROVE_SESSION
                        else "once"
                    ),
                )

            # --------------------------------------------------
            # 5. ALLOW → 真正执行工具
            # --------------------------------------------------

            await self._notify_lifecycle(
                stage="approved",
                tool_name=tool.name,
                call_id=tool_call.id,
                capabilities=capabilities,
                permission_decision=decision.decision.value,
                rule_id=decision.rule_id,
            )
            await self._notify_lifecycle(
                stage="started",
                tool_name=tool.name,
                call_id=tool_call.id,
                capabilities=capabilities,
            )
            started_at = time.monotonic()
            result = await tool.execute(
                tool_call.id,
                tool_call.arguments,
            )

            # --------------------------------------------------
            # 5.1 观察者（optional）：更新 Host 侧观察状态。
            #
            #     observer failure 不能改写 tool 真实结果，
            #     只记录 structured warning，不 silent pass。
            # --------------------------------------------------

            if self._result_observer is not None:
                try:
                    await self._result_observer.on_tool_result(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                    )
                except Exception:
                    logger.exception(
                        "tool result observer 失败 "
                        "(tool=%s, 不影响 tool 结果)",
                        tool_call.name,
                    )

            await self._notify_lifecycle(
                stage="completed",
                tool_name=tool.name,
                call_id=tool_call.id,
                capabilities=capabilities,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )

        # --------------------------------------------------
        # 6. 可预期领域错误
        # --------------------------------------------------

        except CodingAgentError as exc:
            if not terminal_lifecycle_emitted:
                await self._notify_lifecycle(
                    stage="failed",
                    tool_name=tool.name,
                    call_id=tool_call.id,
                    capabilities=capabilities,
                    duration_ms=(
                        int((time.monotonic() - started_at) * 1000)
                        if started_at is not None
                        else 0
                    ),
                    error_code=exc.code.value,
                )
            return ToolResultMessage(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(
                    exc.to_dict(),
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        # --------------------------------------------------
        # 7. 未预期内部错误
        # --------------------------------------------------

        except Exception:
            await self._notify_lifecycle(
                stage="failed",
                tool_name=tool.name,
                call_id=tool_call.id,
                capabilities=capabilities,
                duration_ms=(
                    int((time.monotonic() - started_at) * 1000)
                    if started_at is not None
                    else 0
                ),
                error_code="internal",
            )
            return ToolResultMessage(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(
                    {
                        "code": "internal",
                        "message": "工具执行发生内部错误",
                        "retryable": False,
                        "details": {},
                        "suggested_action": None,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
            )

        # --------------------------------------------------
        # 8. 成功
        # --------------------------------------------------

        return ToolResultMessage(
            tool_call_id=tool_call.id,
            name=tool_call.name,
            content=result.content,
            is_error=False,
        )

    async def dispatch_many(
        self,
        tool_calls: list[ToolCall],
    ) -> list[ToolResultMessage]:
        """Parallelize an all-read-only batch; preserve declared result order."""

        if not tool_calls:
            return []
        tools = [self._registry.get(call.name) for call in tool_calls]
        unsafe_capabilities = {
            "write_fs",
            "edit_file",
            "multi_file_edit",
            "create_file",
            "delete_file",
            "move_file",
            "execute",
        }
        all_read_only = all(
            tool is not None
            and not unsafe_capabilities.intersection(tool.capabilities)
            for tool in tools
        )
        if all_read_only:
            return list(await asyncio.gather(*(self.dispatch(call) for call in tool_calls)))

        results: list[ToolResultMessage] = []
        for call in tool_calls:
            results.append(await self.dispatch(call))
        return results

    async def _notify_lifecycle(self, **payload) -> None:
        if self._lifecycle_observer is None:
            return
        try:
            await self._lifecycle_observer.on_tool_event(**payload)
        except Exception:
            logger.exception("tool lifecycle observer 失败（不影响 tool 结果）")
