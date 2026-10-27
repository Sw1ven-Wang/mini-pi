"""V11 persisted values; runtime clients, futures and locks never belong here."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
import math
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeValue(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_ACCEPTANCE = "waiting_acceptance"
    INTERRUPTED = "interrupted"
    RECONCILING = "reconciling"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class OperationStatus(str, Enum):
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    UNCERTAIN = "uncertain"
    ABANDONED = "abandoned"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    CONSUMED = "consumed"


class RunSnapshot(RuntimeValue):
    format_version: Literal[1] = 1
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    workspace_id: str
    status: RunStatus = RunStatus.QUEUED
    revision: int = Field(default=0, ge=0)
    phase: str | None = None
    cursor: str = "plan"
    proposal_id: str | None = None
    contract_id: str | None = None
    event_sequence: int = 0
    budget: dict[str, int | float] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def valid_budget(self):
        permitted = {"turns", "model_calls", "tool_calls", "replans", "active_seconds", "in_flight", "checkpoint_utc"}
        if any(key not in permitted or not math.isfinite(value) or value < 0 for key, value in self.budget.items()):
            raise ValueError("invalid runtime budget ledger")
        if any(self.budget.get(key, 0) != int(self.budget.get(key, 0))
               for key in ("turns", "model_calls", "tool_calls", "replans")):
            raise ValueError("fractional runtime call reservation")
        return self


class RunStep(RuntimeValue):
    run_id: str
    step_id: str
    plan_revision: int = Field(gt=0)
    status: str
    objective: str
    target_files: list[str] = Field(default_factory=list)


class OperationRecord(RuntimeValue):
    operation_id: str
    run_id: str
    proposal_id: str
    index: int = Field(ge=0)
    kind: Literal["create_file", "edit_file", "delete_file", "move_file"]
    status: OperationStatus
    before: dict[str, str | None]
    after: dict[str, str | None]

    @model_validator(mode="after")
    def consistent_intent(self):
        paths = list(self.before)
        if not paths or set(paths) != set(self.after):
            raise ValueError("inconsistent operation targets")
        if self.kind == "move_file":
            if (len(paths) != 2 or self.before[paths[0]] is None or self.before[paths[1]] is not None
                    or self.after[paths[0]] is not None or self.after[paths[1]] != self.before[paths[0]]):
                raise ValueError("invalid move recovery intent")
        elif len(paths) != 1:
            raise ValueError("invalid file recovery intent")
        else:
            before, after = self.before[paths[0]], self.after[paths[0]]
            if (self.kind == "create_file" and (before is not None or after is None)
                    or self.kind == "edit_file" and (before is None or after is None)
                    or self.kind == "delete_file" and (before is None or after is not None)):
                raise ValueError("invalid file state transition")
        return self


class ApprovalRecord(RuntimeValue):
    approval_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    proposal_id: str
    binding_digest: str
    policy_fingerprint: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    revision: int = 0
    decision: str | None = None
    view: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime


class RuntimeConflictError(RuntimeError):
    """A stale revision or illegal transition must never authorize execution."""


class RuntimePersistenceError(RuntimeError):
    """Fail closed; never substitute an in-memory store."""


class RecoveryRequiredError(RuntimeError):
    """Uncertain effects require reconciliation, not model retries."""


class ExecutionPaused(BaseException):
    """Intentional durable suspension; not an orchestration failure."""

