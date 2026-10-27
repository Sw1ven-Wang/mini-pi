"""Normalized, shell-independent command representation used by policy code."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ShellKind(str, Enum):
    POWERSHELL = "powershell"
    BASH = "bash"


class ParserStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class ShellRedirection(BaseModel):
    operator: str
    target: str | None = None
    fd: str | None = None
    dynamic: bool = False
    start: int = Field(default=0, ge=0)
    end: int = Field(default=0, ge=0)


class ShellCommandNode(BaseModel):
    executable: str | None = None
    argv: list[str] = Field(default_factory=list)
    assignments: dict[str, str] = Field(default_factory=dict)
    wrappers: list[str] = Field(default_factory=list)
    redirections: list[ShellRedirection] = Field(default_factory=list)
    start: int = Field(default=0, ge=0)
    end: int = Field(default=0, ge=0)
    depth: int = Field(default=0, ge=0)
    dynamic: bool = False


class ShellParseResult(BaseModel):
    shell: ShellKind
    status: ParserStatus
    commands: list[ShellCommandNode] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    redirections: list[ShellRedirection] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    command_hash: str

    @property
    def safe_to_classify(self) -> bool:
        return self.status == ParserStatus.OK and bool(self.commands)

