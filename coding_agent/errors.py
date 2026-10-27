"""Coding Agent domain errors."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "invalid_argument"

    PATH_OUTSIDE_WORKSPACE = (
        "path_outside_workspace"
    )

    FILE_NOT_FOUND = "file_not_found"
    FILE_ALREADY_EXISTS = "file_already_exists"

    STALE_VERSION = "stale_version"

    NO_MATCH = "no_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    OVERLAPPING_EDITS = "overlapping_edits"
    NO_OP_EDIT = "no_op_edit"

    INTERNAL = "internal"
    PERMISSION_DENIED = "permission_denied"
    APPROVAL_REQUIRED = "approval_required"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_START_FAILED = "process_start_failed"
    PATCH_RECOVERY_FAILED = "patch_recovery_failed"
    WORKSPACE_RECOVERY_FAILED = "workspace_recovery_failed"
    FILE_NOT_OBSERVED = "file_not_observed"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_PERSISTENCE_FAILED = "session_persistence_failed"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    COMPACTION_INVARIANT = "compaction_invariant"
    SESSION_RECONCILIATION_REQUIRED = (
        "session_reconciliation_required"
    )
    PLANNING_FAILED = "planning_failed"
    EDITING_FAILED = "editing_failed"
    REPLAN_LIMIT_EXCEEDED = "replan_limit_exceeded"
    COMMAND_POLICY_BLOCKED = "command_policy_blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"

class CodingAgentError(Exception):
    """Coding Agent 可预期的领域错误。"""

    code: ErrorCode
    retryable: bool = False
    suggested_action: str | None = None

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)

        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "suggested_action": self.suggested_action,
        }


class InvalidArgumentError(CodingAgentError):
    code = ErrorCode.INVALID_ARGUMENT

    def __init__(
        self,
        message: str,
    ) -> None:
        super().__init__(message)


class PathOutsideWorkspaceError(
    CodingAgentError
):
    code = ErrorCode.PATH_OUTSIDE_WORKSPACE

    def __init__(
        self,
        path: str,
    ) -> None:
        super().__init__(
            "路径位于 workspace 之外",
            details={
                "path": path,
            },
        )


class FileNotFoundAgentError(
    CodingAgentError
):
    code = ErrorCode.FILE_NOT_FOUND

    def __init__(
        self,
        path: str,
    ) -> None:
        super().__init__(
            f"文件不存在: {path}",
            details={
                "path": path,
            },
        )


class FileAlreadyExistsAgentError(
    CodingAgentError
):
    code = ErrorCode.FILE_ALREADY_EXISTS

    suggested_action = (
        "已有文件应先 read，再使用 edit 修改"
    )

    def __init__(
        self,
        path: str,
    ) -> None:
        super().__init__(
            f"文件已存在: {path}",
            details={
                "path": path,
            },
        )


class StaleVersionError(
    CodingAgentError
):
    code = ErrorCode.STALE_VERSION
    retryable = True

    suggested_action = (
        "重新读取文件，获取最新版本后再次编辑"
    )

    def __init__(
        self,
        *,
        expected: str,
        current: str,
    ) -> None:
        super().__init__(
            "文件版本已变化，已拒绝覆盖",
            details={
                "expected": expected,
                "current": current,
            },
        )


class NoMatchError(
    CodingAgentError
):
    code = ErrorCode.NO_MATCH
    retryable = True

    suggested_action = (
        "重新读取目标文件并确认 old_text"
    )

    def __init__(
        self,
        old_text: str,
    ) -> None:
        super().__init__(
            "old_text 未找到",
            details={
                "old_text": old_text,
            },
        )


class AmbiguousMatchError(
    CodingAgentError
):
    code = ErrorCode.AMBIGUOUS_MATCH
    retryable = True

    suggested_action = (
        "提供更完整的 old_text，使匹配位置唯一"
    )

    def __init__(
        self,
        *,
        old_text: str,
        occurrences: int,
    ) -> None:
        super().__init__(
            "old_text 存在多个匹配位置",
            details={
                "old_text": old_text,
                "occurrences": occurrences,
            },
        )


class OverlappingEditsError(
    CodingAgentError
):
    code = ErrorCode.OVERLAPPING_EDITS
    retryable = True

    suggested_action = (
        "重新规划 edits，确保多个修改范围互不重叠"
    )

    def __init__(
        self,
        *,
        first_index: int,
        second_index: int,
    ) -> None:
        super().__init__(
            "多个 edit 的修改范围发生重叠",
            details={
                "first_edit": first_index,
                "second_edit": second_index,
            },
        )


class NoOpEditError(
    CodingAgentError
):
    code = ErrorCode.NO_OP_EDIT
    retryable = False

    def __init__(self) -> None:
        super().__init__(
            "old_text 与 new_text 相同，修改不会产生变化"
        )

class PermissionDeniedError(
    CodingAgentError
):
    code = ErrorCode.PERMISSION_DENIED
    retryable = False

    def __init__(
        self,
        *,
        tool: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"权限拒绝: {reason}",
            details={
                "tool": tool,
                "reason": reason,
            },
        )


class ApprovalRequiredError(
    CodingAgentError
):
    code = ErrorCode.APPROVAL_REQUIRED
    retryable = False

    suggested_action = (
        "需要用户批准后才能继续执行"
    )

    def __init__(
        self,
        *,
        tool: str,
        reason: str,
    ) -> None:
        super().__init__(
            f"操作需要批准: {reason}",
            details={
                "tool": tool,
                "reason": reason,
            },
        )
class ProcessTimeoutError(
    CodingAgentError
):
    code = ErrorCode.PROCESS_TIMEOUT
    retryable = False

    suggested_action = (
        "检查命令是否卡住，"
        "或明确调整 timeout 后重新执行"
    )

    def __init__(
        self,
        *,
        command: str,
        timeout_seconds: float,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(
            f"命令执行超过 {timeout_seconds} 秒",
            details={
                "command": command,
                "timeout_seconds": timeout_seconds,
                "stdout": stdout,
                "stderr": stderr,
            },
        )


class ProcessStartFailedError(
    CodingAgentError
):
    code = ErrorCode.PROCESS_START_FAILED
    retryable = False

    suggested_action = (
        "检查 shell executable 是否可用，"
        "或确认运行环境依赖已安装"
    )

    def __init__(
        self,
        *,
        command: str,
        executable: str,
        reason: str = "",
    ) -> None:
        super().__init__(
            f"进程启动失败: {executable}",
            details={
                "command": command,
                "executable": executable,
                "reason": reason,
            },
        )


class PatchRecoveryError(
    CodingAgentError
):
    """多文件 patch 部分失败后，rollback 未能恢复某些文件。"""

    code = ErrorCode.PATCH_RECOVERY_FAILED
    retryable = False

    suggested_action = (
        "部分文件修改已落盘且无法自动回滚，"
        "请人工检查这些文件的状态"
    )

    def __init__(
        self,
        *,
        unrecovered_files: list[str],
        cause: str = "",
    ) -> None:
        super().__init__(
            "patch 部分失败且 rollback 未完全恢复",
            details={
                "unrecovered_files": unrecovered_files,
                "cause": cause,
            },
        )


class WorkspaceRecoveryError(
    CodingAgentError
):
    """workspace mutation 部分失败后，recovery 未完全恢复。

    details 同时保留：
        primary_error   —— 原始 apply operation 为什么失败
        recovery_report —— recovery 哪些成功/失败
    """

    code = ErrorCode.WORKSPACE_RECOVERY_FAILED
    retryable = False

    suggested_action = (
        "部分文件变更已落盘且无法自动恢复，"
        "请根据 recovery report 人工检查"
    )

    def __init__(
        self,
        *,
        report: Any,
        primary_error: Any = None,
        cause: str = "",
    ) -> None:
        super().__init__(
            "workspace mutation 部分失败且 recovery 未完全恢复",
            details={
                "primary_error": (
                    primary_error.model_dump()
                    if hasattr(primary_error, "model_dump")
                    else primary_error
                ),
                "recovery_report": report.model_dump(),
                "cause": cause,
            },
        )


class FileNotObservedError(
    CodingAgentError
):
    """mutation 前必须先 read，Host 没有该文件的观察版本。"""

    code = ErrorCode.FILE_NOT_OBSERVED
    retryable = True

    suggested_action = (
        "先使用 read 读取文件，再执行修改/删除操作"
    )

    def __init__(self, *, path: str) -> None:
        super().__init__(
            f"文件未被观察，请先 read: {path}",
            details={"path": path},
        )


class SessionNotFoundError(CodingAgentError):
    code = ErrorCode.SESSION_NOT_FOUND

    def __init__(self, *, session_id: str) -> None:
        super().__init__(
            f"Session 不存在: {session_id}",
            details={"session_id": session_id},
        )


class SessionPersistenceError(CodingAgentError):
    code = ErrorCode.SESSION_PERSISTENCE_FAILED

    suggested_action = (
        "检查 session 存储目录；runtime 状态可能已经继续推进"
    )

    def __init__(
        self,
        message: str,
        *,
        session_id: str,
    ) -> None:
        super().__init__(
            message,
            details={"session_id": session_id},
        )


class ContextBudgetExceededError(CodingAgentError):
    code = ErrorCode.CONTEXT_BUDGET_EXCEEDED

    def __init__(
        self,
        *,
        estimated_tokens: int,
        available_tokens: int,
    ) -> None:
        super().__init__(
            "压缩后的 Context 仍超过可用预算",
            details={
                "estimated_tokens": estimated_tokens,
                "available_tokens": available_tokens,
            },
        )


class CompactionInvariantError(CodingAgentError):
    code = ErrorCode.COMPACTION_INVARIANT

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SessionReconciliationRequiredError(CodingAgentError):
    code = ErrorCode.SESSION_RECONCILIATION_REQUIRED
    retryable = True

    suggested_action = (
        "先使用 read/search/glob/powershell 检查当前磁盘状态，再继续"
    )

    def __init__(self, *, session_id: str) -> None:
        super().__init__(
            "中断 Session 尚未完成磁盘状态 reconciliation",
            details={"session_id": session_id},
        )


class PlanningError(CodingAgentError):
    code = ErrorCode.PLANNING_FAILED
    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message)


class EditingError(CodingAgentError):
    code = ErrorCode.EDITING_FAILED
    retryable = True

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ReplanLimitExceededError(CodingAgentError):
    code = ErrorCode.REPLAN_LIMIT_EXCEEDED
    retryable = False

    def __init__(self, *, max_replans: int) -> None:
        super().__init__(
            f"Replan 次数已达到上限 {max_replans}",
            details={"max_replans": max_replans},
        )


class CommandPolicyBlockedError(CodingAgentError):
    code = ErrorCode.COMMAND_POLICY_BLOCKED
    retryable = False

    def __init__(
        self,
        *,
        risk: str,
        rule_id: str,
        reason: str,
        suggestion: str | None = None,
    ) -> None:
        super().__init__(
            f"CommandPolicy 拒绝命令: {reason}",
            details={"risk": risk, "rule_id": rule_id},
        )
        self.suggested_action = suggestion


class BudgetExhaustedError(CodingAgentError):
    code = ErrorCode.BUDGET_EXHAUSTED
    retryable = False

    def __init__(self, *, budget: str, limit: int | float) -> None:
        super().__init__(
            f"Task budget exhausted: {budget}",
            details={"budget": budget, "limit": limit},
        )


class SandboxUnavailableError(CodingAgentError):
    code = ErrorCode.SANDBOX_UNAVAILABLE
    retryable = False

    def __init__(self, *, reason: str, engine: str | None = None) -> None:
        super().__init__(
            f"L2 sandbox 不可用: {reason}",
            details={"engine": engine, "reason": reason},
        )
