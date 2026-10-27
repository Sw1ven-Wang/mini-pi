"""Single-user Web transport adapter around the authoritative V11 RuntimeKernel."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.approvals import ApprovalDecision
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.observability.metrics import MetricsSink, RunMetrics
from coding_agent.observability.redaction import redact_text
from coding_agent.observability.tracing import TraceContext
from coding_agent.orchestration import (
    OrchestrationPhase,
    TaskOutcome,
)
from coding_agent.safety.factory import permission_engine_from_workspace
from coding_agent.session import CodingSession, SessionSnapshot, SessionStatus
from coding_agent.session_store import SessionStore
from coding_agent.web.approval import WebApprovalHandler
from coding_agent.web.config import ProviderRegistry
from coding_agent.web.directories import DirectoryBrowser
from coding_agent.web.events import WebEventSink
from coding_agent.web.models import (
    ApprovalView,
    SessionSummary,
    TaskCreateRequest,
    TaskResultView,
    TaskSnapshot,
    RecoveryView,
    WebTaskStatus,
)
from coding_agent.web.previews import InMemoryPreviewStore
from coding_agent.runtime.kernel import RuntimeKernel, default_state_root, workspace_identity
from coding_agent.runtime.factory import build_coding_runtime
from coding_agent.runtime.models import (
    ExecutionPaused, RunStatus, TERMINAL, RecoveryRequiredError, RuntimeConflictError,
)
from coding_agent.runtime.acceptance import workspace_fingerprint, default_contract
from coding_agent.errors import WorkspaceRecoveryError
from coding_agent.mutation.planner import plan_workspace_change_set
from coding_agent.mutation.preview import build_mutation_preview
from coding_agent.runtime.privacy import is_link
from coding_agent.runtime.store import SCHEMA_VERSION


class ActiveTaskError(RuntimeError):
    pass


class TaskNotFoundError(KeyError):
    pass


class TaskStateError(RuntimeError):
    pass


@dataclass
class TaskRecord:
    session_id: str
    goal: str
    root_id: str
    workspace_path: str
    workspace_root: Path
    provider_id: str
    model_id: str
    status: WebTaskStatus = WebTaskStatus.QUEUED
    current_phase: OrchestrationPhase | None = None
    created_at: datetime = field(default_factory=lambda: _now())
    updated_at: datetime = field(default_factory=lambda: _now())
    session: CodingSession | None = None
    outcome: TaskOutcome | None = None
    error: str | None = None
    terminal_event_emitted: bool = False
    asyncio_task: asyncio.Task | None = None
    trace_context: TraceContext = field(default_factory=TraceContext.root)
    metrics_sink: MetricsSink = field(default_factory=MetricsSink)
    preview_store: InMemoryPreviewStore = field(default_factory=InMemoryPreviewStore)
    event_sink: WebEventSink | None = None
    approval_handler: WebApprovalHandler | None = None
    execution: Any = None


class TaskManager:
    def __init__(
        self,
        *,
        directories: DirectoryBrowser,
        providers: ProviderRegistry,
        verify_command: str,
        max_event_buffer: int = 500,
        state_root: Path | None = None,
        acceptance_contract=None,
    ) -> None:
        self.directories = directories
        self.providers = providers
        self.verify_command = verify_command
        self.max_event_buffer = max_event_buffer
        self._records: dict[str, TaskRecord] = {}
        self._active_session_id: str | None = None
        self._lock = asyncio.Lock()
        self.state_root = state_root or default_state_root()
        self._kernels: dict[str, RuntimeKernel] = {}
        self.acceptance_contract = acceptance_contract
        self._discover()

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    async def create(self, request: TaskCreateRequest) -> TaskSnapshot:
        workspace_root = self.directories.resolve(
            request.root_id, request.workspace_path,
        )
        async with self._lock:
            self._ensure_idle()
            provider, model = self.providers.create(
                request.provider_id, request.model_id,
            )
            kernel = self._kernel(workspace_root)
            session_id = str(uuid4())
            execution = kernel.start(session_id, command=self.verify_command, contract=self.acceptance_contract, data={
                "web": {"root_id": request.root_id, "workspace_path": request.workspace_path,
                    "provider_id": request.provider_id, "model_id": request.model_id,
                    "goal": redact_text(request.goal)},
            })
            record = self._new_record(
                session_id=session_id,
                goal=request.goal,
                root_id=request.root_id,
                workspace_path=_relative_workspace_path(
                    self.directories, request.root_id, workspace_root,
                ),
                workspace_root=workspace_root,
                provider_id=request.provider_id,
                model_id=request.model_id,
                execution=execution,
            )
            self._records[record.session_id] = record
            self._active_session_id = record.session_id
            record.asyncio_task = asyncio.create_task(
                self._run(record, provider=provider, model=model, resume=False),
                name=f"mini-pi-web-{record.session_id}",
            )
        return self.snapshot(record.session_id)

    async def resume(
        self,
        *,
        session_id: str,
        root_id: str,
        workspace_path: str,
    ) -> TaskSnapshot:
        workspace_root = self.directories.resolve(root_id, workspace_path)
        kernel = self._kernel(workspace_root)
        store = kernel.session_store
        if not store.exists(session_id):
            stored = self._load_session(self._session_store(workspace_root), session_id)
            store.import_legacy(stored)
            if stored.state.status == SessionStatus.ACTIVE:
                raise TaskStateError("legacy interrupted session requires explicit reconciliation")
        stored = store.load(session_id)
        if stored.state.metadata.get("v11_import", {}).get("requires_reconciliation"):
            raise TaskStateError("legacy interrupted session requires explicit reconciliation")
        if stored.state.status == SessionStatus.COMPLETED:
            raise TaskStateError("completed sessions cannot be resumed")
        provider_id, model_id = _web_model_metadata(stored)
        async with self._lock:
            previous_record = self._records.get(session_id)
            if self._active_session_id != session_id:
                self._ensure_idle()
            elif previous_record and previous_record.asyncio_task and not previous_record.asyncio_task.done():
                raise ActiveTaskError("task still has an executor")
            provider, model = self.providers.create(provider_id, model_id)
            runs = kernel.store.runs(session_id)
            if runs and runs[-1].status not in TERMINAL:
                execution = kernel.resume(runs[-1].run_id)
            else:
                execution = kernel.start(session_id, command=self.verify_command, contract=self.acceptance_contract, data={
                    "web": {"root_id": root_id, "workspace_path": workspace_path,
                        "provider_id": provider_id, "model_id": model_id, "goal": redact_text(stored.state.user_goal)}})
            record = self._new_record(
                session_id=session_id,
                goal=stored.state.user_goal,
                root_id=root_id,
                workspace_path=_relative_workspace_path(
                    self.directories, root_id, workspace_root,
                ),
                workspace_root=workspace_root,
                provider_id=provider_id,
                model_id=model_id,
                execution=execution,
            )
            # A paused browser may still subscribe to the previous transport.
            # End that stream so EventSource reconnects to the replacement sink.
            if previous_record and previous_record.event_sink:
                await previous_record.event_sink.close()
            self._records[record.session_id] = record
            self._active_session_id = record.session_id
            record.asyncio_task = asyncio.create_task(
                self._run(record, provider=provider, model=model, resume=True),
                name=f"mini-pi-web-{record.session_id}",
            )
        return self.snapshot(record.session_id)

    def snapshot(self, session_id: str) -> TaskSnapshot:
        record = self._record(session_id)
        session = record.session
        metrics = record.metrics_sink.snapshot(record.trace_context.trace_id)
        result = None
        if record.outcome is not None:
            result = TaskResultView(
                passed=record.outcome.passed,
                decision=record.outcome.decision,
                changed_files=record.outcome.changed_files,
                trace=record.outcome.trace,
                metrics=record.outcome.metrics,
            )
        run = record.execution.run if record.execution is not None else None
        if run is not None:
            for name, count in run.data.get("runtime_metrics", {}).items():
                if name in type(metrics).model_fields:
                    setattr(metrics, name, count)
            metrics.model_calls = max(metrics.model_calls, int(run.budget.get("model_calls", 0)))
            metrics.tool_calls = max(metrics.tool_calls, int(run.budget.get("tool_calls", 0)))
            metrics.replans = int(run.budget.get("replans", 0))
            metrics.duration_ms = max(metrics.duration_ms, int(run.budget.get("active_seconds", 0) * 1000))
        if run is not None and result is None and run.data.get("outcome"):
            result = TaskResultView.model_validate(run.data["outcome"])
        acceptance = None
        recovery = None
        if record.execution is not None:
            try:
                fingerprint = (record.execution.workspace_digest()
                    if run.cursor in {"acceptance", "final"} else "unverified")
            except Exception:
                # A bad workspace must invalidate evidence, not hide the read-only
                # recovery report. Execution still performs strict revalidation.
                fingerprint = "invalid_workspace"
            acceptance = record.execution.acceptance.evaluate(fingerprint)
            if run.status in {RunStatus.INTERRUPTED, RunStatus.NEEDS_RECONCILIATION}:
                operations = []
                for item in record.execution.store.values("operation", run.run_id):
                    from coding_agent.runtime.models import OperationRecord
                    try:
                        operation = OperationRecord.model_validate(item)
                        targets = [record.execution.workspace.relative(record.execution.workspace.resolve(path))
                                   for path in operation.before]
                        operations.append({"kind": operation.kind, "status": operation.status.value, "targets": targets})
                    except Exception:
                        operations.append({"kind": "invalid", "status": "uncertain", "targets": []})
                from coding_agent.runtime.recovery import assess_recovery
                try:
                    self._bind_current_policy(record)
                    assessment = assess_recovery(record.execution)
                    if not self._host_contract_matches(record):
                        assessment["can_retry_verification"] = False
                        assessment["blockers"].append("host_contract_changed")
                except (RuntimeError, OSError, ValueError):
                    assessment = {"blockers": ["environment_unverifiable"]}
                recovery = RecoveryView(cursor=run.cursor, reason=record.error,
                    operations=operations, **assessment)
        return TaskSnapshot(
            session_id=record.session_id,
            goal=record.goal,
            root_id=record.root_id,
            workspace_path=record.workspace_path,
            provider_id=record.provider_id,
            model_id=record.model_id,
            status=record.status,
            current_phase=record.current_phase,
            permission_mode=(
                session.state.permission_mode if session else "default"
            ),
            plan=session.state.current_plan if session else None,
            metrics=metrics,
            result=result,
            pending_approval=(
                record.approval_handler.pending
                if record.approval_handler is not None else None
            ),
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
            run_id=run.run_id if run else None,
            runtime_status=run.status.value if run else None,
            revision=run.revision if run else 0,
            event_sequence=run.event_sequence if run else 0,
            recovery_required=bool(run and (run.status in {RunStatus.INTERRUPTED, RunStatus.NEEDS_RECONCILIATION}
                or (run.status == RunStatus.WAITING_APPROVAL and record.approval_handler.pending is None))),
            acceptance=acceptance,
            acceptance_contract=record.execution.acceptance.contract() if record.execution else None,
            recovery=recovery,
        )

    def event_sink(self, session_id: str) -> WebEventSink:
        record = self._record(session_id)
        assert record.event_sink is not None
        return record.event_sink

    def preview(self, session_id: str, preview_id: str | None = None):
        return self._record(session_id).preview_store.get(preview_id)

    def approve(
        self,
        session_id: str,
        approval_id: str,
        decision: ApprovalDecision,
        expected_revision: int | None = None,
    ) -> TaskSnapshot:
        record = self._record(session_id)
        if record.approval_handler is None:
            raise TaskStateError("task has no approval handler")
        try:
            if record.execution is not None:
                if expected_revision is not None and record.execution.run.revision != expected_revision:
                    raise RuntimeConflictError("stale approval revision")
                self._bind_current_policy(record)
            record.approval_handler.resolve(approval_id, decision)
        except (KeyError, RuntimeError) as exc:
            raise TaskStateError(str(exc)) from exc
        record.updated_at = _now()
        if record.execution is not None:
            if decision == ApprovalDecision.DENY:
                record.execution.update(status=RunStatus.FAILED, event="approval_denied")
                record.status = WebTaskStatus.FAILED
                if record.session is not None:
                    record.session.persist_runtime_state(status=SessionStatus.FAILED)
                record.asyncio_task = asyncio.create_task(self._finish_record(record))
            else:
                record.asyncio_task = asyncio.create_task(self._continue(record))
        return self.snapshot(session_id)

    def confirm_acceptance(self, session_id, requirement_id, expected_revision):
        record = self._record(session_id)
        execution = record.execution
        with execution.store.transaction():
            if execution.run.status != RunStatus.WAITING_ACCEPTANCE or execution.run.revision != expected_revision:
                raise TaskStateError("acceptance is stale or not pending")
            self._bind_current_policy(record)
            fingerprint = execution.workspace_digest()
            execution.acceptance.confirm(requirement_id, fingerprint)
            execution.update(event="manual_acceptance_confirmed", payload={"requirement_id": requirement_id})
        record.asyncio_task = asyncio.create_task(self._continue(record))
        return self.snapshot(session_id)

    async def reconcile(self, session_id, action, expected_revision):
        async with self._lock:
            record = self._record(session_id)
            if record.asyncio_task is not None and not record.asyncio_task.done():
                raise TaskStateError("task still has an executor")
            try:
                self._bind_current_policy(record)
                if action == "retry_verification" and not self._host_contract_matches(record):
                    raise RecoveryRequiredError("host acceptance configuration changed")
                record.execution.kernel.reconcile(record.execution.run_id,
                    action=action, expected_revision=expected_revision,
                    permission_engine=record.execution.permission_engine)
            except (RuntimeError, OSError, ValueError) as exc:
                raise TaskStateError(str(exc)) from exc
            record.error = None
            if action == "retry_verification":
                record.asyncio_task = asyncio.create_task(self._continue(record))
            else:
                record.status = WebTaskStatus.CANCELLED
        if action == "end_preserving_files":
            await self._finish_record(record)
        return self.snapshot(session_id)

    async def cancel(self, session_id: str) -> TaskSnapshot:
        record = self._record(session_id)
        if record.status not in {
            WebTaskStatus.QUEUED,
            WebTaskStatus.RUNNING,
            WebTaskStatus.AWAITING_APPROVAL,
        } and not (record.execution and record.execution.run.status in {
                RunStatus.WAITING_ACCEPTANCE, RunStatus.INTERRUPTED}):
            raise TaskStateError("task is not active")
        if record.approval_handler is not None:
            record.approval_handler.deny_pending()
        if record.asyncio_task is not None:
            record.asyncio_task.cancel()
            try:
                await record.asyncio_task
            except asyncio.CancelledError:
                pass
        if record.execution is not None and record.execution.run.status not in TERMINAL:
            try:
                record.execution.kernel.cancel(record.execution.run_id,
                    executor_stopped=record.asyncio_task is not None and record.asyncio_task.done())
            except RuntimeError as exc:
                raise TaskStateError(str(exc)) from exc
        record.status = WebTaskStatus.CANCELLED
        record.updated_at = _now()
        # A Task cancelled before its first step never enters _run's finally.
        # Complete the lifecycle here as well; finalization is idempotent.
        await self._finish_record(record)
        return self.snapshot(session_id)

    def list_sessions(self, root_id: str, workspace_path: str) -> list[SessionSummary]:
        workspace = self.directories.resolve(root_id, workspace_path)
        kernel = self._kernel(workspace)
        root = self._session_store(workspace).root
        summaries: list[SessionSummary] = []
        snapshots = [SessionSnapshot.model_validate(value) for value in kernel.store.values("session")]
        for session_path in root.glob("*/session.json") if root.is_dir() else []:
            try:
                if not session_path.resolve(strict=True).is_relative_to(
                    root.resolve(strict=True)
                ):
                    continue
                snapshot = SessionSnapshot.model_validate_json(
                    session_path.read_bytes()
                )
                if not kernel.session_store.exists(snapshot.state.session_id):
                    snapshots.append(snapshot)
            except Exception:
                continue
        for snapshot in snapshots:
            try:
                metadata = snapshot.state.metadata.get("web", {})
                summaries.append(SessionSummary(
                    session_id=snapshot.state.session_id,
                    goal=snapshot.state.user_goal,
                    status=snapshot.state.status,
                    updated_at=snapshot.state.updated_at,
                    plan_revision=snapshot.state.plan_revision,
                    provider_id=metadata.get("provider_id"),
                    model_id=metadata.get("model_id"),
                ))
            except (ValueError, TypeError):
                continue
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries[:100]

    def _new_record(self, **values: Any) -> TaskRecord:
        record = TaskRecord(**values)

        async def on_event(event: AgentEvent) -> None:
            self._update_from_event(record, event)

        async def on_approval(pending: ApprovalView | None) -> None:
            record.status = (
                WebTaskStatus.AWAITING_APPROVAL
                if pending is not None else WebTaskStatus.RUNNING
            )
            record.updated_at = _now()
            if pending is not None and record.event_sink is not None:
                await record.event_sink.emit(record.trace_context.event(
                    session_id=record.session_id,
                    type=AgentEventType.PHASE_CHANGED,
                    payload={
                        "phase": (
                            record.current_phase or OrchestrationPhase.MUTATE
                        ).value,
                        "approval_pending": True,
                    },
                ))

        record.event_sink = WebEventSink(
            max_events=self.max_event_buffer,
            on_event=on_event,
            runtime_execution=record.execution,
        )
        record.approval_handler = WebApprovalHandler(on_approval,
            durable=record.execution.approvals if record.execution is not None else None)
        return record

    async def _run(self, record: TaskRecord, *, provider, model, resume: bool) -> None:
        event_bus = EventBus([record.event_sink, record.metrics_sink])
        try:
            if not self._host_contract_matches(record):
                raise RecoveryRequiredError("host acceptance configuration changed")
            # Re-resolve through the startup whitelist on every executor entry.
            workspace = self.directories.resolve(record.root_id, record.workspace_path)
            if workspace_identity(workspace) != record.execution.kernel.workspace_id:
                raise RecoveryRequiredError("workspace identity changed")
            session, orchestrator = build_coding_runtime(
                execution=record.execution, provider=provider, model=model,
                approval_handler=record.approval_handler, verify_command=self.verify_command,
                event_bus=event_bus, trace_context=record.trace_context,
                preview_observer=record.preview_store, resume=resume, goal=record.goal)
            if not resume:
                session.state.metadata["web"] = {
                    "provider_id": record.provider_id,
                    "model_id": record.model_id,
                }
                session.persist_runtime_state()
            record.session = session
            record.status = WebTaskStatus.RUNNING
            record.updated_at = _now()
            record.outcome = await record.execution.kernel.execute(record.execution, orchestrator, session)
            record.status = WebTaskStatus.COMPLETED
        except ExecutionPaused:
            record.status = (WebTaskStatus.AWAITING_APPROVAL
                if record.execution.run.status == RunStatus.WAITING_APPROVAL else WebTaskStatus.FAILED)
        except asyncio.CancelledError:
            record.status = WebTaskStatus.CANCELLED
            if record.session is not None:
                record.session.persist_runtime_state(status=SessionStatus.CANCELLED)
            if not record.terminal_event_emitted:
                assert record.event_sink is not None
                await record.event_sink.emit(record.trace_context.event(
                    session_id=record.session_id,
                    type=AgentEventType.TASK_CANCELLED,
                    payload={},
                ))
        except BaseException as exc:
            record.status = WebTaskStatus.FAILED
            if record.execution is not None and record.execution.run.status not in TERMINAL:
                from coding_agent.runtime.recovery import resources_stopped
                record.execution.update(status=(RunStatus.NEEDS_RECONCILIATION
                    if isinstance(exc, (RecoveryRequiredError, WorkspaceRecoveryError)) or not resources_stopped(record.execution)[0]
                    else RunStatus.FAILED), event="execution_failed",
                    payload={"error_type": type(exc).__name__})
            message = redact_text(str(exc))
            for workspace_value in {
                str(record.workspace_root),
                str(record.workspace_root).replace("\\", "/"),
            }:
                message = message.replace(workspace_value, "<workspace>")
            record.error = message[:1000] or type(exc).__name__
            if record.execution is not None:
                record.execution.update(data={**record.execution.run.data, "error": record.error}, event="error_recorded")
            if not record.terminal_event_emitted:
                assert record.event_sink is not None
                await record.event_sink.emit(record.trace_context.event(
                    session_id=record.session_id,
                    type=AgentEventType.TASK_FAILED,
                    payload={"error_type": type(exc).__name__},
                ))
        finally:
            await self._finish_record(record)

    async def _finish_record(self, record: TaskRecord) -> None:
        record.updated_at = _now()
        assert record.event_sink is not None
        if record.status == WebTaskStatus.CANCELLED and not record.terminal_event_emitted:
            await record.event_sink.emit(record.trace_context.event(
                session_id=record.session_id,
                type=AgentEventType.TASK_CANCELLED,
                payload={},
            ))
        if record.execution is not None and record.execution.run.status not in TERMINAL:
            return  # Paused/uncertain runs keep the workspace slot, not a coroutine.
        await record.event_sink.close()
        async with self._lock:
            if self._active_session_id == record.session_id and self._records.get(record.session_id) is record:
                self._active_session_id = None

    async def _continue(self, record):
        try:
            record.execution = record.execution.kernel.resume(record.execution.run_id)
            record.approval_handler.durable = record.execution.approvals
            record.event_sink.runtime_execution = record.execution
            provider, model = self.providers.create(record.provider_id, record.model_id)
            await self._run(record, provider=provider, model=model, resume=True)
        except BaseException as exc:
            record.error = redact_text(str(exc))[:1000]
            record.status = WebTaskStatus.FAILED
            if record.execution.run.status == RunStatus.RUNNING:
                record.execution.update(status=RunStatus.NEEDS_RECONCILIATION, event="resume_failed")

    def _bind_current_policy(self, record):
        workspace = self.directories.resolve(record.root_id, record.workspace_path)
        if workspace_identity(workspace) != record.execution.kernel.workspace_id:
            raise TaskStateError("workspace identity changed")
        engine = permission_engine_from_workspace(workspace)
        saved = record.execution.kernel.session_store.load(record.session_id)
        engine.safety_constraints = saved.state.safety_constraints
        if (engine.settings.permission_mode.value == "auto"
                and saved.state.permission_mode.value == "default" and saved.state.auto_total_blocks):
            engine.mode = saved.state.permission_mode
        engine.update_user_request(saved.state.user_goal)
        record.execution.permission_engine = engine

    def _host_contract_matches(self, record):
        expected = self.acceptance_contract or default_contract(self.verify_command)
        return (expected.model_dump(mode="json", exclude={"contract_id"}) ==
                record.execution.acceptance.contract().model_dump(mode="json", exclude={"contract_id"}))

    def _kernel(self, workspace):
        identity = workspace_identity(workspace)
        if identity not in self._kernels:
            self._kernels[identity] = RuntimeKernel(Workspace(workspace), state_root=self.state_root)
        return self._kernels[identity]

    def _discover(self):
        if not self.state_root.is_dir():
            return
        for database in self.state_root.glob("*/runtime.db"):
            if any(is_link(item) for item in (database, database.parent, *database.parent.parents)):
                raise RuntimeConflictError("linked runtime database")
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (1, SCHEMA_VERSION):
                    raise RuntimeConflictError("unsupported runtime schema")
                rows = db.execute("SELECT payload FROM runs ORDER BY rowid").fetchall()
            for row in rows:
                value = json.loads(row[0])
                metadata = value.get("data", {}).get("web")
                if not metadata:
                    continue
                try:
                    workspace = self.directories.resolve(metadata["root_id"], metadata["workspace_path"])
                except (KeyError, ValueError, PermissionError, OSError):
                    if RunStatus(value["status"]) not in TERMINAL:
                        raise RuntimeConflictError("an unresolved run is outside the current workspace whitelist")
                    continue  # A removed whitelist entry never grants access.
                if workspace_identity(workspace) != value["workspace_id"]:
                    if RunStatus(value["status"]) not in TERMINAL:
                        raise RuntimeConflictError("an unresolved workspace was replaced; explicit reconciliation required")
                    continue
                kernel = self._kernel(workspace)
                kernel.mark_interrupted()
                execution = kernel.execution(value["run_id"])
                from coding_agent.runtime.recovery import resources_stopped
                if execution.run.status in TERMINAL and not resources_stopped(execution)[0]:
                    raise RuntimeConflictError("terminal legacy run has uncertain resources; offline reconciliation required")
                record = self._new_record(session_id=value["session_id"], goal=metadata["goal"],
                    workspace_root=workspace, root_id=metadata["root_id"], workspace_path=metadata["workspace_path"],
                    provider_id=metadata["provider_id"], model_id=metadata["model_id"], execution=execution)
                if kernel.session_store.exists(record.session_id):
                    # Snapshot does not instantiate a Provider or executor.
                    snapshot = kernel.session_store.load(record.session_id)
                    record.session = _StoredSessionView(snapshot, kernel.session_store)
                record.created_at = execution.run.created_at
                record.updated_at = execution.run.updated_at
                record.error = execution.run.data.get("error")
                status = execution.run.status
                record.status = {
                    RunStatus.SUCCEEDED: WebTaskStatus.COMPLETED,
                    RunStatus.CANCELLED: WebTaskStatus.CANCELLED,
                    RunStatus.WAITING_APPROVAL: WebTaskStatus.AWAITING_APPROVAL,
                }.get(status, WebTaskStatus.FAILED)
                if execution.run.phase:
                    record.current_phase = OrchestrationPhase(execution.run.phase)
                if status == RunStatus.WAITING_APPROVAL:
                    try:
                        execution.validate_proposal()
                        preview = build_mutation_preview(plan_workspace_change_set(execution.workspace, execution.proposal()))
                        record.preview_store.publish_sync(preview)
                        pending = execution.approvals.pending()
                        if pending:
                            pending.view["preview_id"] = preview.preview_id
                            execution.store.put("approval", pending.approval_id, pending.model_dump(mode="json"), execution.run_id)
                    except Exception:
                        execution.approvals.invalidate_pending()
                        execution.update(status=RunStatus.NEEDS_RECONCILIATION, event="approval_recovery_blocked")
                        record.status = WebTaskStatus.FAILED
                        record.error = "审批对应的文件已变化，需要重新对账。"
                self._records[record.session_id] = record
        unresolved = [record for record in self._records.values() if record.execution.run.status not in TERMINAL]
        if len(unresolved) > 1:
            raise RuntimeConflictError("multiple unresolved runs require reconciliation before startup")
        if unresolved:
            self._active_session_id = unresolved[0].session_id

    async def shutdown(self):
        # Graceful server restarts are interruptions, not user cancellations.
        for record in self._records.values():
            task = record.asyncio_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if record.execution.run.status == RunStatus.RUNNING:
                    record.execution.update(status=RunStatus.INTERRUPTED, event="server_stopped")
            if record.event_sink is not None:
                await record.event_sink.close()
        for kernel in self._kernels.values():
            kernel.close()

    def _update_from_event(self, record: TaskRecord, event: AgentEvent) -> None:
        if event.type == AgentEventType.TASK_STARTED:
            record.status = WebTaskStatus.RUNNING
        elif event.type == AgentEventType.PHASE_CHANGED:
            try:
                record.current_phase = OrchestrationPhase(event.payload.get("phase"))
            except ValueError:
                pass
        elif event.type == AgentEventType.TASK_COMPLETED:
            record.status = WebTaskStatus.COMPLETED
            record.terminal_event_emitted = True
        elif event.type == AgentEventType.TASK_FAILED:
            record.status = WebTaskStatus.FAILED
            record.terminal_event_emitted = True
        elif event.type == AgentEventType.TASK_CANCELLED:
            record.status = WebTaskStatus.CANCELLED
            record.terminal_event_emitted = True
        record.updated_at = event.timestamp

    def _record(self, session_id: str) -> TaskRecord:
        record = self._records.get(session_id)
        if record is None:
            raise TaskNotFoundError(session_id)
        return record

    def _ensure_idle(self) -> None:
        if self._active_session_id is not None:
            raise ActiveTaskError(
                f"task {self._active_session_id} is still active"
            )

    @staticmethod
    def _session_store(workspace: Path) -> SessionStore:
        safe_root = Workspace(workspace).resolve(".mini_pi/web_sessions")
        return SessionStore(safe_root)

    @staticmethod
    def _load_session(store: SessionStore, session_id: str) -> SessionSnapshot:
        candidate = (store.root / session_id / "session.json").resolve(strict=False)
        if not candidate.is_relative_to(store.root.resolve(strict=False)):
            raise TaskStateError("session path escapes workspace state root")
        return store.load(session_id)


def _relative_workspace_path(
    directories: DirectoryBrowser,
    root_id: str,
    workspace: Path,
) -> str:
    root = next(item for item in directories.roots if item.id == root_id)
    relative = workspace.relative_to(root.path).as_posix()
    return "" if relative == "." else relative


def _web_model_metadata(snapshot: SessionSnapshot) -> tuple[str, str]:
    metadata = snapshot.state.metadata.get("web")
    if not isinstance(metadata, dict):
        raise TaskStateError("session has no web provider metadata")
    provider_id = metadata.get("provider_id")
    model_id = metadata.get("model_id")
    if not isinstance(provider_id, str) or not isinstance(model_id, str):
        raise TaskStateError("session web provider metadata is incomplete")
    return provider_id, model_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _StoredSessionView:
    """Read projection during restart discovery; never creates a model client."""
    def __init__(self, snapshot, store):
        self.snapshot, self.state, self.store = snapshot, snapshot.state, store

    def persist_runtime_state(self, status=None):
        if status is not None:
            self.state.status = status
        self.store.save(self.snapshot)
