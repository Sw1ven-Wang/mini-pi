"""Public request and response models for the Mini-Pi workbench."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agent.approvals import ApprovalDecision
from coding_agent.agents.models import TaskPlan, VerificationDecision
from coding_agent.mutation.preview import MutationPreview
from coding_agent.observability.metrics import RunMetrics
from coding_agent.orchestration import OrchestrationMetrics, OrchestrationPhase
from coding_agent.safety.settings import PermissionMode
from coding_agent.session import SessionStatus
from coding_agent.runtime.acceptance import AcceptanceResult, AcceptanceContract


class WebTaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkspaceRootView(BaseModel):
    id: str
    label: str


class DirectoryEntry(BaseModel):
    name: str
    relative_path: str


class DirectoryListing(BaseModel):
    root_id: str
    relative_path: str
    parent_path: str | None = None
    directories: list[DirectoryEntry] = Field(default_factory=list)


class ProviderProfileView(BaseModel):
    id: str
    label: str
    models: list[str]
    default_model: str
    configured: bool


class BootstrapResponse(BaseModel):
    csrf_token: str
    roots: list[WorkspaceRootView]
    providers: list[ProviderProfileView]
    verify_command: str
    active_session_id: str | None = None


class TaskCreateRequest(BaseModel):
    root_id: str = Field(min_length=1, max_length=128)
    workspace_path: str = Field(default="", max_length=2048)
    goal: str = Field(min_length=1, max_length=20_000)
    provider_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("goal cannot be blank")
        return normalized


class ResumeRequest(BaseModel):
    root_id: str = Field(min_length=1, max_length=128)
    workspace_path: str = Field(default="", max_length=2048)


class ApprovalDecisionRequest(BaseModel):
    decision: ApprovalDecision
    expected_revision: int | None = Field(default=None, ge=0)


class AcceptanceConfirmationRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class ReconciliationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    expected_revision: int = Field(ge=0)
    action: Literal["retry_verification", "end_preserving_files"]


class ApprovalView(BaseModel):
    approval_id: str
    tool_name: str
    reason: str
    rule_id: str
    matched_rule_ids: list[str] = Field(default_factory=list)
    risk: str | None = None
    normalized_targets: list[str] = Field(default_factory=list)
    summary: str | None = None
    preview_id: str | None = None


class TaskResultView(BaseModel):
    passed: bool
    decision: VerificationDecision
    changed_files: list[str] = Field(default_factory=list)
    trace: list[OrchestrationPhase] = Field(default_factory=list)
    metrics: OrchestrationMetrics


class RecoveryView(BaseModel):
    cursor: str
    reason: str | None = None
    operations: list[dict[str, str | list[str]]] = Field(default_factory=list)
    external_resources: list[dict[str, str | list[str]]] = Field(default_factory=list)
    can_retry_verification: bool = False
    can_end_preserving_files: bool = False
    blockers: list[str] = Field(default_factory=list)


class TaskSnapshot(BaseModel):
    session_id: str
    goal: str
    root_id: str
    workspace_path: str
    provider_id: str
    model_id: str
    status: WebTaskStatus
    current_phase: OrchestrationPhase | None = None
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    plan: TaskPlan | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    result: TaskResultView | None = None
    pending_approval: ApprovalView | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    run_id: str | None = None
    runtime_status: str | None = None
    revision: int = 0
    event_sequence: int = 0
    recovery_required: bool = False
    acceptance: AcceptanceResult | None = None
    acceptance_contract: AcceptanceContract | None = None
    recovery: RecoveryView | None = None


class SessionSummary(BaseModel):
    session_id: str
    goal: str
    status: SessionStatus
    updated_at: datetime
    plan_revision: int = 0
    provider_id: str | None = None
    model_id: str | None = None


class ChangePreviewResponse(BaseModel):
    preview: MutationPreview | None = None
