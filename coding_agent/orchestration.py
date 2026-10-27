"""V7 Planner → Viewer → Editor → Verifier state machine."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from enum import Enum
from time import perf_counter

from pydantic import BaseModel, Field

from agent.approvals import ApprovalDecision, ApprovalRequest
from agent.permissions import PermissionDecisionType
from agent.tools import AgentTool, AgentToolResult

from coding_agent.agents.editor import Editor
from coding_agent.agents.models import (
    ChangeIntent,
    EditorRequest,
    EvidenceBundle,
    PlanStepStatus,
    PlannerContext,
    TaskPlan,
    VerificationDecision,
)
from coding_agent.agents.planner import Planner
from coding_agent.agents.verifier import Verifier
from coding_agent.agents.viewer import Viewer
from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.context_budget import ApproxTokenEstimator
from coding_agent.diagnostics.models import Diagnostic, DiagnosticSeverity
from coding_agent.errors import (
    ApprovalRequiredError, CodingAgentError, EditingError, ErrorCode,
    PermissionDeniedError,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.models import WorkspaceMutationResult
from coding_agent.mutation.planner import plan_workspace_change_set
from coding_agent.mutation.preview import (
    MutationPreviewObserver,
    build_mutation_preview,
)
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.observations import (
    ObservationSource,
    ObservedFileState,
)
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.tracing import TraceContext
from coding_agent.observability.runtime import MutationEventObserver
from coding_agent.replay.recorder import TraceRecorder
from coding_agent.replanning import ReplanController
from coding_agent.repository_map import RepositoryMapBuilder
from coding_agent.session import CodingSession, SessionStatus
from coding_agent.verification.models import VerificationScope
from coding_agent.task_budget import BudgetTracker, TaskBudget
from coding_agent.runtime.models import ExecutionPaused, RunStatus
from coding_agent.runtime.budgets import DurableBudgetTracker
from coding_agent.runtime.acceptance import workspace_fingerprint


class OrchestrationPhase(str, Enum):
    RECEIVE_TASK = "receive_task"
    PLAN = "plan"
    GATHER_EVIDENCE = "gather_evidence"
    EDIT = "edit"
    MUTATE = "mutate"
    VERIFY = "verify"
    REPLAN = "replan"
    FINAL = "final"


class OrchestrationMetrics(BaseModel):
    task_success: bool = False
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    editor_input_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    read_calls: int = Field(default=0, ge=0)
    search_calls: int = Field(default=0, ge=0)
    files_read: int = Field(default=0, ge=0)
    mutation_attempts: int = Field(default=0, ge=0)
    no_match_count: int = Field(default=0, ge=0)
    ambiguous_match_count: int = Field(default=0, ge=0)
    stale_version_count: int = Field(default=0, ge=0)
    verification_attempts: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)


class TaskOutcome(BaseModel):
    passed: bool
    plan: TaskPlan
    evidence: EvidenceBundle
    decision: VerificationDecision
    changed_files: list[str] = Field(default_factory=list)
    trace: list[OrchestrationPhase]
    metrics: OrchestrationMetrics


class _WorkspaceMutationPermissionTool(AgentTool):
    """Permission-only adapter; mutation execution stays Host-owned."""

    name = "workspace_mutation"
    description = "Apply a validated WorkspaceChangeSet"
    parameters = {"type": "object"}

    def __init__(self, workspace: Workspace, change_set: WorkspaceChangeSet) -> None:
        self.workspace = workspace
        capabilities = {"write_fs"}
        if len(change_set.operations) > 1:
            capabilities.add("multi_file_edit")
        capabilities.update(operation.kind for operation in change_set.operations)
        self.capabilities = frozenset(capabilities)

    async def execute(self, tool_call_id, arguments) -> AgentToolResult:
        raise RuntimeError("permission adapter is never executed")


class CodingOrchestrator:
    """Host-owned V7 phase orchestration, separate from ``agent_loop.py``."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        planner: Planner,
        viewer: Viewer,
        editor: Editor,
        mutation_runtime: WorkspaceMutationRuntime,
        verifier: Verifier,
        repository_map_builder: RepositoryMapBuilder,
        replan_controller: ReplanController | None = None,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
        task_budget: TaskBudget | None = None,
        trace_recorder: TraceRecorder | None = None,
        mutation_preview_observer: MutationPreviewObserver | None = None,
        runtime_execution=None,
    ) -> None:
        if planner.provider is not editor.provider:
            raise ValueError("Planner 与 Editor 必须共享同一 Provider 实例")
        if planner.model is not editor.model:
            raise ValueError("Planner 与 Editor 必须共享同一 Model 实例")
        self.workspace = workspace
        self.planner = planner
        self.viewer = viewer
        self.editor = editor
        self.mutation_runtime = mutation_runtime
        self.verifier = verifier
        self.repository_map_builder = repository_map_builder
        self.replan_controller = replan_controller or ReplanController()
        self.estimator = ApproxTokenEstimator()
        self.event_bus = event_bus or EventBus()
        self.trace_context = trace_context
        self._active_trace: TraceContext | None = None
        self._active_session_id: str | None = None
        self.task_budget = task_budget or TaskBudget()
        self._budget_tracker: BudgetTracker | None = None
        self.trace_recorder = trace_recorder
        self.mutation_preview_observer = mutation_preview_observer
        self.runtime_execution = runtime_execution

    async def run_task(
        self,
        goal: str,
        session: CodingSession,
    ) -> TaskOutcome:
        if not goal:
            raise ValueError("goal 不能为空")

        started = perf_counter()
        trace = [OrchestrationPhase.RECEIVE_TASK]
        metrics = OrchestrationMetrics()
        diagnostics: list[Diagnostic] = []
        changed_files: list[str] = []
        files_read_seen: set[str] = set()
        self._active_trace = self.trace_context or TraceContext.root()
        self._active_session_id = session.session_id
        execution = self.runtime_execution
        self._budget_tracker = (
            DurableBudgetTracker(self.task_budget, execution.store, execution.run_id)
            if execution is not None else BudgetTracker(self.task_budget)
        )
        if execution is not None:
            execution.permission_engine = session.tool_dispatcher.permission_engine
            execution.budget_tracker = self._budget_tracker
            changed_files = list(execution.run.data.get("changed_files", []))
            metrics.replan_count = self._budget_tracker.replans
        if self.trace_recorder is not None and self.trace_recorder not in self.event_bus.sinks:
            self.event_bus.sinks.append(self.trace_recorder)
        if hasattr(self.mutation_runtime, "set_lifecycle_observer"):
            self.mutation_runtime.set_lifecycle_observer(MutationEventObserver(
                event_bus=self.event_bus,
                session_id=session.session_id,
                trace=self._active_trace,
            ))
        if hasattr(self.viewer, "budget_tracker"):
            self.viewer.budget_tracker = self._budget_tracker
        if isinstance(self.viewer, Viewer):
            self.viewer.permission_engine = session.tool_dispatcher.permission_engine
        session.persist_runtime_state(status=SessionStatus.ACTIVE)
        await self._emit(AgentEventType.TASK_STARTED, {"goal_present": True})
        await self._emit(
            AgentEventType.PHASE_CHANGED,
            {"phase": OrchestrationPhase.RECEIVE_TASK.value},
        )

        try:
            if execution is not None and execution.run.proposal_id is not None:
                recovered = await self._resume_durable_cycle(
                    goal, session, diagnostics, changed_files, trace, metrics, started,
                )
                if recovered is not None:
                    return recovered
            if isinstance(self.viewer, Viewer):
                self.repository_map_builder.allowed_paths = await self.viewer.readable_paths()
            plan = session.state.current_plan
            if plan is None:
                trace.append(OrchestrationPhase.PLAN)
                await self._emit(
                    AgentEventType.PHASE_CHANGED,
                    {"phase": OrchestrationPhase.PLAN.value},
                )
                plan = await self._plan(
                    goal=goal,
                    session=session,
                    diagnostics=diagnostics,
                    metrics=metrics,
                )
                session.record_plan(plan)
                await self._emit(
                    AgentEventType.PLAN_CREATED,
                    {"revision": plan.revision, "step_count": len(plan.steps)},
                )

            while True:
                self._budget_tracker.consume_turn()
                self._budget_tracker.check_duration()
                trace.append(OrchestrationPhase.GATHER_EVIDENCE)
                await self._emit(
                    AgentEventType.PHASE_CHANGED,
                    {"phase": OrchestrationPhase.GATHER_EVIDENCE.value},
                )
                evidence = await self._budget_tracker.run(
                    self.viewer.gather(
                        plan,
                        repository_notes=self.repository_map_builder.build().notes,
                    )
                )
                self._record_viewer_metrics(
                    plan, evidence, metrics, files_read_seen
                )

                evidence_assessment = self.replan_controller.assess(
                    evidence=evidence
                )
                if evidence_assessment.should_replan:
                    plan = await self._replan(
                        goal=goal,
                        session=session,
                        previous=plan,
                        reason=evidence_assessment.reason,
                        diagnostics=diagnostics,
                        trace=trace,
                        metrics=metrics,
                    )
                    continue

                request = EditorRequest(
                    intent=_intent_from_plan(plan),
                    evidence=evidence,
                )
                trace.append(OrchestrationPhase.EDIT)
                await self._emit(
                    AgentEventType.PHASE_CHANGED,
                    {"phase": OrchestrationPhase.EDIT.value},
                )
                try:
                    change_set = await self._edit(
                        request=request,
                        diagnostics=diagnostics,
                        metrics=metrics,
                    )
                except EditingError:
                    assessment = self.replan_controller.assess(
                        assumption_invalid=True
                    )
                    plan = await self._replan(
                        goal=goal,
                        session=session,
                        previous=plan,
                        reason=assessment.reason,
                        diagnostics=diagnostics,
                        trace=trace,
                        metrics=metrics,
                    )
                    continue

                trace.append(OrchestrationPhase.MUTATE)
                await self._emit(
                    AgentEventType.PHASE_CHANGED,
                    {"phase": OrchestrationPhase.MUTATE.value},
                )
                metrics.mutation_attempts += 1
                metrics.tool_calls += 1
                if execution is not None:
                    execution.prepare_proposal(change_set)
                try:
                    preview_id = await self._publish_mutation_preview(change_set)
                except CodingAgentError as exc:
                    self._record_mutation_error(exc, metrics)
                    assessment = self.replan_controller.assess(error=exc)
                    if not assessment.should_replan:
                        raise
                    plan = await self._replan(
                        goal=goal,
                        session=session,
                        previous=plan,
                        reason=assessment.reason,
                        diagnostics=diagnostics,
                        trace=trace,
                        metrics=metrics,
                    )
                    continue
                await self._emit(
                    AgentEventType.MUTATION_PLANNED,
                    {
                        "operation_count": len(change_set.operations),
                        "paths": _change_paths(change_set),
                        "plan_revision": plan.revision,
                        "reason_step_ids": [step.id for step in plan.steps],
                    },
                )
                await self._authorize_mutation(
                    change_set,
                    session,
                    preview_id=preview_id,
                )
                if self.trace_recorder is not None:
                    self.trace_recorder.record_action(
                        change_set,
                        safe_arguments={
                            "paths": _change_paths(change_set),
                            "plan_revision": plan.revision,
                        },
                    )
                try:
                    self._budget_tracker.consume_tool_call()
                    if execution is not None:
                        execution.before_apply(change_set, self.mutation_runtime)
                    mutation = await self._budget_tracker.run(
                        self.mutation_runtime.apply(change_set)
                    )
                    if execution is not None:
                        execution.mutation_complete(mutation)
                except CodingAgentError as exc:
                    await self._emit(
                        AgentEventType.MUTATION_ROLLED_BACK,
                        {"success": False, "error_code": exc.code.value},
                    )
                    self._record_mutation_error(exc, metrics)
                    assessment = self.replan_controller.assess(error=exc)
                    if not assessment.should_replan:
                        raise
                    plan = await self._replan(
                        goal=goal,
                        session=session,
                        previous=plan,
                        reason=assessment.reason,
                        diagnostics=diagnostics,
                        trace=trace,
                        metrics=metrics,
                    )
                    continue

                await self._emit(
                    AgentEventType.MUTATION_APPLIED,
                    {
                        "changed_files": list(mutation.changed_files),
                        "mutation_count": len(mutation.mutations),
                        "versions": [
                            {
                                "path": item.path,
                                "before": item.old_version.digest if item.old_version else None,
                                "after": item.new_version.digest if item.new_version else None,
                                "mutation_id": item.checkpoint_id,
                            }
                            for item in mutation.mutations
                        ],
                    },
                )
                changed_files.extend(mutation.changed_files)
                self._record_observations(change_set, mutation, session)
                session.persist_runtime_state()

                trace.append(OrchestrationPhase.VERIFY)
                await self._emit(
                    AgentEventType.PHASE_CHANGED,
                    {"phase": OrchestrationPhase.VERIFY.value},
                )
                metrics.verification_attempts += 1
                metrics.tool_calls += 1
                await self._emit(
                    AgentEventType.VERIFICATION_STARTED,
                    {"criteria_count": len(plan.acceptance_criteria)},
                )
                self._budget_tracker.consume_tool_call()
                verification_fingerprint = execution.begin_verification() if execution is not None else None
                decision = await self._budget_tracker.run(
                    self.verifier.verify(
                        workspace=self.workspace,
                        acceptance_criteria=plan.acceptance_criteria,
                        scope=VerificationScope(
                            changed_files=mutation.changed_files,
                            changed_ranges=[
                                changed_range
                                for item in mutation.mutations
                                for changed_range in item.changed_ranges
                            ],
                        ),
                        baseline_diagnostics=diagnostics,
                    )
                )
                if execution is not None:
                    acceptance = execution.verification_complete(decision, verification_fingerprint)
                    if decision.passed and not acceptance.passed:
                        execution.update(status=RunStatus.WAITING_ACCEPTANCE, event="acceptance_pending")
                        raise ExecutionPaused()
                await self._emit(
                    AgentEventType.VERIFICATION_COMPLETED,
                    {
                        "passed": decision.passed,
                        "diagnostic_count": len(decision.diagnostics),
                    },
                )
                if self.trace_recorder is not None:
                    self.trace_recorder.set_verification_summary(
                        decision.model_dump(mode="json")
                    )
                if decision.passed:
                    return await self._complete_success(
                        session, plan, evidence, decision, changed_files, trace, metrics, started,
                    )

                new_error = _has_new_error(decision.diagnostics, diagnostics)
                diagnostics = decision.diagnostics
                assessment = self.replan_controller.assess(
                    verification=decision,
                    new_error_detected=new_error,
                )
                plan = await self._replan(
                    goal=goal,
                    session=session,
                    previous=plan,
                    reason=assessment.reason,
                    diagnostics=diagnostics,
                    trace=trace,
                    metrics=metrics,
                )
        except ExecutionPaused:
            self._budget_tracker.save()
            session.persist_runtime_state()
            raise
        except asyncio.CancelledError:
            metrics.total_duration_ms = _elapsed_ms(started)
            session.persist_runtime_state(status=SessionStatus.CANCELLED)
            await self._emit(
                AgentEventType.TASK_CANCELLED,
                {"duration_ms": metrics.total_duration_ms},
                root=True,
            )
            raise

        except BaseException as exc:
            metrics.total_duration_ms = _elapsed_ms(started)
            session.persist_runtime_state(status=SessionStatus.FAILED)
            await self._emit(
                AgentEventType.TASK_FAILED,
                {"error_type": type(exc).__name__, "duration_ms": metrics.total_duration_ms},
                root=True,
            )
            raise

    async def _complete_success(self, session, plan, evidence, decision, changed_files, trace, metrics, started):
        completed = plan.model_copy(update={"steps": [
            step.model_copy(update={"status": PlanStepStatus.COMPLETED}) for step in plan.steps]})
        trace.append(OrchestrationPhase.FINAL)
        metrics.task_success = True
        metrics.total_duration_ms = _elapsed_ms(started)
        outcome = TaskOutcome(passed=True, plan=completed, evidence=evidence, decision=decision,
            changed_files=list(dict.fromkeys(changed_files)), trace=trace, metrics=metrics)
        execution = self.runtime_execution
        if execution is not None:
            metrics.model_calls = self._budget_tracker.model_calls
            metrics.replan_count = self._budget_tracker.replans
        with execution.store.transaction() if execution is not None else nullcontext():
            session.record_plan(completed)
            session.persist_runtime_state(status=SessionStatus.COMPLETED)
            if execution is not None:
                execution.succeed(outcome)
        await self._emit(AgentEventType.PHASE_CHANGED, {"phase": OrchestrationPhase.FINAL.value})
        await self._emit(AgentEventType.TASK_COMPLETED, {
            "passed": True, "revision": completed.revision, "duration_ms": metrics.total_duration_ms,
        }, root=True)
        return outcome

    async def _resume_durable_cycle(self, goal, session, diagnostics, changed_files, trace, metrics, started):
        execution = self.runtime_execution
        plan = session.state.current_plan
        if plan is None:
            raise ValueError("durable proposal has no associated plan")
        change_set = execution.proposal()
        if execution.run.cursor == "proposal":
            trace.append(OrchestrationPhase.MUTATE)
            await self._emit(AgentEventType.PHASE_CHANGED, {"phase": OrchestrationPhase.MUTATE.value})
            preview_id = await self._publish_mutation_preview(change_set)
            await self._authorize_mutation(change_set, session, preview_id=preview_id)
            execution.before_apply(change_set, self.mutation_runtime)
            self._budget_tracker.consume_tool_call()
            mutation = await self._budget_tracker.run(self.mutation_runtime.apply(change_set))
            execution.mutation_complete(mutation)
        else:
            mutation = execution.restored_mutation()
        changed_files.extend(mutation.changed_files)
        self._record_observations(change_set, mutation, session)
        session.persist_runtime_state()
        if execution.run.cursor == "acceptance":
            decision = VerificationDecision.model_validate(execution.store.get("verification_decision", execution.run_id))
            acceptance = execution.acceptance.evaluate(execution.workspace_digest())
        else:
            trace.append(OrchestrationPhase.VERIFY)
            await self._emit(AgentEventType.PHASE_CHANGED, {"phase": OrchestrationPhase.VERIFY.value})
            metrics.verification_attempts += 1
            self._budget_tracker.consume_tool_call()
            fingerprint = execution.begin_verification()
            decision = await self._budget_tracker.run(self.verifier.verify(workspace=self.workspace,
                acceptance_criteria=plan.acceptance_criteria,
                scope=VerificationScope(changed_files=mutation.changed_files), baseline_diagnostics=diagnostics))
            acceptance = execution.verification_complete(decision, fingerprint)
            await self._emit(AgentEventType.VERIFICATION_COMPLETED, {"passed": decision.passed,
                "diagnostic_count": len(decision.diagnostics)})
        if decision.passed:
            if not acceptance.passed:
                execution.update(status=RunStatus.WAITING_ACCEPTANCE, event="acceptance_pending")
                raise ExecutionPaused()
            return await self._complete_success(session, plan, EvidenceBundle(), decision,
                changed_files, trace, metrics, started)
        await self._replan(goal=goal, session=session, previous=plan,
            reason=decision.reason, diagnostics=decision.diagnostics, trace=trace, metrics=metrics)
        return None

    async def _authorize_mutation(
        self,
        change_set: WorkspaceChangeSet,
        session: CodingSession,
        *,
        preview_id: str | None = None,
    ) -> None:
        tool = _WorkspaceMutationPermissionTool(self.workspace, change_set)
        paths = _change_paths(change_set)
        arguments = {
            "files": [{"path": path} for path in paths],
            "operations": [
                {"kind": operation.kind} for operation in change_set.operations
            ],
        }
        engine = session.tool_dispatcher.permission_engine
        decision = await engine.evaluate(tool, arguments)
        policy_payload = {
            "tool_name": tool.name,
            "permission_decision": decision.decision.value,
            "rule_id": decision.rule_id,
            "matched_rule_ids": decision.matched_rule_ids,
            "risk": decision.risk,
            "parser_status": decision.parser_status,
            "command_hash": decision.command_hash,
            "operations": decision.operations,
            "normalized_targets": decision.normalized_targets,
        }
        await self._emit(AgentEventType.POLICY_EVALUATED, policy_payload)
        if decision.rule_id.startswith("auto_"):
            await self._emit(AgentEventType.AUTO_CLASSIFIED, {
                **policy_payload,
                "duration_ms": decision.classifier_latency_ms,
                "classifier_result": decision.classifier_result,
                "classifier_stage": decision.classifier_stage,
                "classifier_input_tokens": decision.classifier_input_tokens,
                "classifier_output_tokens": decision.classifier_output_tokens,
            })

        if decision.decision == PermissionDecisionType.DENY:
            raise PermissionDeniedError(tool=tool.name, reason=decision.reason)
        if decision.decision != PermissionDecisionType.ASK:
            return

        await self._emit(AgentEventType.APPROVAL_REQUESTED, policy_payload)
        approval_handler = session.tool_dispatcher.approval_handler
        if approval_handler is None:
            raise ApprovalRequiredError(tool=tool.name, reason=decision.reason)
        approval = await approval_handler.request(ApprovalRequest(
            tool_name=tool.name,
            arguments=arguments,
            reason=decision.reason,
            rule_id=decision.rule_id,
            matched_rule_ids=decision.matched_rule_ids,
            risk=decision.risk,
            normalized_targets=decision.normalized_targets,
            summary=decision.approval_summary,
            preview_id=preview_id,
        ))
        allowed = approval != ApprovalDecision.DENY
        await self._emit(AgentEventType.APPROVAL_RESOLVED, {
            **policy_payload,
            "permission_decision": "allow" if allowed else "deny",
            "approval_scope": (
                "session" if approval == ApprovalDecision.APPROVE_SESSION else "once"
            ),
        })
        if not allowed:
            raise PermissionDeniedError(
                tool=tool.name, reason="用户拒绝了 workspace mutation",
            )
        if approval == ApprovalDecision.APPROVE_SESSION:
            remember = getattr(engine, "remember_approval", None)
            if callable(remember):
                remember(tool, arguments, decision)
                if self.runtime_execution is not None:
                    self.runtime_execution.save_grants(session.session_id)

    async def _publish_mutation_preview(
        self,
        change_set: WorkspaceChangeSet,
    ) -> str | None:
        if self.mutation_preview_observer is None:
            return None
        planned = plan_workspace_change_set(self.workspace, change_set)
        preview = build_mutation_preview(planned)
        await self.mutation_preview_observer.publish(preview)
        return preview.preview_id

    async def _plan(
        self,
        *,
        goal: str,
        session: CodingSession,
        diagnostics: list[Diagnostic],
        metrics: OrchestrationMetrics,
        replan_reason: str | None = None,
    ) -> TaskPlan:
        repository_summary = self.repository_map_builder.build().render_summary()
        context = PlannerContext(
            constraints=_metadata_strings(session.state.metadata, "constraints"),
            session_summary=_session_summary(
                session, current_replan_reason=replan_reason
            ),
            repository_map_summary=repository_summary,
            unresolved_diagnostics=diagnostics,
        )
        metrics.model_calls += 1
        assert self._budget_tracker is not None
        self._budget_tracker.consume_model_call()
        metrics.input_tokens += self.estimator.estimate_text(
            json.dumps(
                {"goal": goal, **context.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
        await self._emit(
            AgentEventType.MODEL_REQUEST_STARTED, {"phase": "planner"}
        )
        try:
            plan = await self._budget_tracker.run(
                self.planner.create_plan(goal=goal, context=context)
            )
        except BaseException as exc:
            await self._emit(
                AgentEventType.MODEL_REQUEST_FAILED,
                {"phase": "planner", "error_type": type(exc).__name__},
            )
            raise
        await self._emit(
            AgentEventType.MODEL_REQUEST_COMPLETED, {"phase": "planner"}
        )
        metrics.output_tokens += self.estimator.estimate_text(
            plan.model_dump_json()
        )
        return plan

    async def _edit(
        self,
        *,
        request: EditorRequest,
        diagnostics: list[Diagnostic],
        metrics: OrchestrationMetrics,
    ) -> WorkspaceChangeSet:
        estimated = self.estimator.estimate_text(
            json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "diagnostics": [
                        item.model_dump(mode="json") for item in diagnostics
                    ],
                },
                ensure_ascii=False,
            )
        )
        metrics.model_calls += 1
        assert self._budget_tracker is not None
        self._budget_tracker.consume_model_call()
        metrics.input_tokens += estimated
        metrics.editor_input_tokens += estimated
        await self._emit(
            AgentEventType.MODEL_REQUEST_STARTED, {"phase": "editor"}
        )
        try:
            change_set = await self._budget_tracker.run(
                self.editor.create_change_set(
                    request=request,
                    diagnostics=diagnostics,
                )
            )
        except BaseException as exc:
            await self._emit(
                AgentEventType.MODEL_REQUEST_FAILED,
                {"phase": "editor", "error_type": type(exc).__name__},
            )
            raise
        await self._emit(
            AgentEventType.MODEL_REQUEST_COMPLETED, {"phase": "editor"}
        )
        metrics.output_tokens += self.estimator.estimate_text(
            change_set.model_dump_json()
        )
        return change_set

    async def _replan(
        self,
        *,
        goal: str,
        session: CodingSession,
        previous: TaskPlan,
        reason: str,
        diagnostics: list[Diagnostic],
        trace: list[OrchestrationPhase],
        metrics: OrchestrationMetrics,
    ) -> TaskPlan:
        self.replan_controller.ensure_allowed(metrics.replan_count)
        assert self._budget_tracker is not None
        self._budget_tracker.consume_replan()
        if self.runtime_execution is not None:
            self.runtime_execution.replan()
        trace.append(OrchestrationPhase.REPLAN)
        await self._emit(
            AgentEventType.PHASE_CHANGED,
            {"phase": OrchestrationPhase.REPLAN.value},
        )
        candidate = await self._plan(
            goal=goal,
            session=session,
            diagnostics=diagnostics,
            metrics=metrics,
            replan_reason=reason,
        )
        revised = self.replan_controller.advance_revision(
            candidate,
            previous=previous,
            replan_count=metrics.replan_count,
        )
        metrics.replan_count += 1
        session.record_plan(revised, replan_reason=reason)
        await self._emit(
            AgentEventType.PLAN_REVISED,
            {"revision": revised.revision, "reason": reason},
        )
        return revised

    async def _emit(
        self,
        event_type: AgentEventType,
        payload: dict,
        *,
        root: bool = False,
    ) -> None:
        if self._active_trace is None or self._active_session_id is None:
            return
        trace = self._active_trace if root else self._active_trace.child()
        event = trace.event(session_id=self._active_session_id, type=event_type, payload=payload)
        if self.runtime_execution is not None:
            changes = {"phase": payload["phase"]} if event_type == AgentEventType.PHASE_CHANGED else {}
            self.runtime_execution.update(event="agent_event", payload={"agent_event": event.model_dump(mode="json")}, **changes)
        await self.event_bus.emit(event)

    @staticmethod
    def _record_viewer_metrics(
        plan: TaskPlan,
        evidence: EvidenceBundle,
        metrics: OrchestrationMetrics,
        files_read_seen: set[str],
    ) -> None:
        targets = list(
            dict.fromkeys(path for step in plan.steps for path in step.target_files)
        )
        queries = list(
            dict.fromkeys(
                query for step in plan.steps for query in step.evidence_queries
            )
        )
        glob_calls = sum(any(char in target for char in "*?[") for target in targets)
        metrics.search_calls += len(queries)
        read_paths = {item.path for item in evidence.snippets}
        metrics.read_calls += len(read_paths)
        files_read_seen.update(read_paths)
        metrics.files_read = len(files_read_seen)
        metrics.tool_calls += glob_calls + len(queries) + len(
            read_paths
        )

    @staticmethod
    def _record_mutation_error(
        error: CodingAgentError,
        metrics: OrchestrationMetrics,
    ) -> None:
        if error.code == ErrorCode.NO_MATCH:
            metrics.no_match_count += 1
        elif error.code == ErrorCode.AMBIGUOUS_MATCH:
            metrics.ambiguous_match_count += 1
        elif error.code == ErrorCode.STALE_VERSION:
            metrics.stale_version_count += 1

    @staticmethod
    def _record_observations(
        change_set: WorkspaceChangeSet,
        result: WorkspaceMutationResult,
        session: CodingSession,
    ) -> None:
        versions = {item.path: item.new_version for item in result.mutations}
        now = datetime.now(timezone.utc)
        for operation in change_set.operations:
            if operation.kind == "delete_file":
                session.observations.update(
                    ObservedFileState(
                        path=operation.path,
                        exists=False,
                        source=ObservationSource.DELETE,
                        observed_at=now,
                    )
                )
            elif operation.kind == "move_file":
                session.observations.update(
                    ObservedFileState(
                        path=operation.source,
                        exists=False,
                        source=ObservationSource.MOVE,
                        observed_at=now,
                    )
                )
                version = versions.get(operation.destination)
                if version is not None:
                    session.observations.update(
                        ObservedFileState(
                            path=operation.destination,
                            exists=True,
                            version=version,
                            source=ObservationSource.MOVE,
                            observed_at=now,
                        )
                    )
            else:
                version = versions.get(operation.path)
                if version is not None:
                    session.observations.update(
                        ObservedFileState(
                            path=operation.path,
                            exists=True,
                            version=version,
                            source=(
                                ObservationSource.CREATE
                                if operation.kind == "create_file"
                                else ObservationSource.EDIT
                            ),
                            observed_at=now,
                        )
                    )


def _intent_from_plan(plan: TaskPlan) -> ChangeIntent:
    return ChangeIntent(
        objective=plan.objective,
        target_files=list(
            dict.fromkeys(path for step in plan.steps for path in step.target_files)
        ),
        requirements=[step.objective for step in plan.steps],
        invariants=list(plan.constraints),
        acceptance_criteria=list(plan.acceptance_criteria),
    )


def _metadata_strings(metadata: dict, key: str) -> list[str]:
    value = metadata.get(key, [])
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _session_summary(
    session: CodingSession,
    *,
    current_replan_reason: str | None = None,
) -> str | None:
    parts = [
        item
        for item in (
            session.state.compacted_summary,
            session.state.latest_replan_reason,
            current_replan_reason,
        )
        if item
    ]
    return "\n".join(parts) or None


def _has_new_error(
    current: list[Diagnostic],
    previous: list[Diagnostic],
) -> bool:
    previous_payloads = {item.model_dump_json() for item in previous}
    return any(
        item.severity == DiagnosticSeverity.ERROR
        and item.model_dump_json() not in previous_payloads
        for item in current
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _change_paths(change_set: WorkspaceChangeSet) -> list[str]:
    paths: list[str] = []
    for operation in change_set.operations:
        if operation.kind == "move_file":
            paths.extend([operation.source, operation.destination])
        else:
            paths.append(operation.path)
    return list(dict.fromkeys(paths))
