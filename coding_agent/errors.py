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