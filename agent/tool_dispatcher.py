"""Tool dispatcher."""

from __future__ import annotations

import json

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
    InvalidArgumentError,
    PermissionDeniedError,
)

from .tool_registry import ToolRegistry
from agent.approvals import (
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
)

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
        ) -> None:

            self._registry = registry
            self._permission_engine = (
                permission_engine
            )

            self._approval_handler = (
                approval_handler
            )

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def dispatch(
        self,
        tool_call: ToolCall,
    ) -> ToolResultMessage:

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

            # --------------------------------------------------
            # 3. DENY
            # --------------------------------------------------

            if (
                decision.decision
                == PermissionDecisionType.DENY
            ):
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
                 # 没有交互式审批能力
                if self._approval_handler is None:
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
                        )
                    )
                )

                if (
                    approval
                    == ApprovalDecision.DENY
                ):
                    raise PermissionDeniedError(
                        tool=tool.name,
                        reason="用户拒绝了该操作",
                    )

            # --------------------------------------------------
            # 5. ALLOW → 真正执行工具
            # --------------------------------------------------

            result = await tool.execute(
                tool_call.id,
                tool_call.arguments,
            )

        # --------------------------------------------------
        # 6. 可预期领域错误
        # --------------------------------------------------

        except CodingAgentError as exc:
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