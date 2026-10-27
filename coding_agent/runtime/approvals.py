"""Approval decisions survive restarts; authorization still requires fresh checks."""

from datetime import timedelta

from agent.approvals import ApprovalDecision
from coding_agent.observability.redaction import redact
from coding_agent.runtime.models import (
    ApprovalRecord, ApprovalStatus as A, RunStatus, RuntimeConflictError, now,
    ExecutionPaused,
)
from agent.approvals import ApprovalHandler


class DurableApprovalService:
    def __init__(self, execution):
        self.execution = execution
        self.store = execution.store

    def pending(self) -> ApprovalRecord | None:
        for value in reversed(self.store.values("approval", self.execution.run_id)):
            record = ApprovalRecord.model_validate(value)
            if record.status == A.PENDING and record.expires_at > now():
                return record
        return None

    def request(self, request) -> ApprovalRecord:
        execution = self.execution
        binding = execution.validate_proposal()
        policy = execution.policy_fingerprint()
        proposal_id = execution.run.proposal_id
        for value in reversed(self.store.values("approval", execution.run_id)):
            record = ApprovalRecord.model_validate(value)
            if record.proposal_id != proposal_id:
                continue
            if record.status not in {A.PENDING, A.APPROVED, A.CONSUMED}:
                continue
            if record.status == A.CONSUMED and any(row["status"] != "prepared" for row in execution.journal().rows()):
                continue
            if record.expires_at <= now():
                if record.status != A.CONSUMED:
                    self._save(record, A.EXPIRED)
                continue
            if record.binding_digest != binding or record.policy_fingerprint != policy:
                if record.status != A.CONSUMED:
                    self._save(record, A.INVALIDATED)
                continue
            # Preview is ephemeral: refresh its id without changing the proposal.
            record.view["preview_id"] = request.preview_id
            self.store.put("approval", record.approval_id, record.model_dump(mode="json"), execution.run_id)
            if record.status == A.CONSUMED:
                execution.update(event="approval_lease_recovered", payload={"approval_id": record.approval_id})
                return record.model_copy(update={"status": A.APPROVED})
            if record.status == A.PENDING:
                execution.update(status=RunStatus.WAITING_APPROVAL, event="approval_wait_restored")
            return record
        record = ApprovalRecord(run_id=execution.run_id, proposal_id=proposal_id,
            binding_digest=binding, policy_fingerprint=policy,
            expires_at=now() + timedelta(hours=24), view=redact({
                "tool_name": request.tool_name, "reason": request.reason,
                "rule_id": request.rule_id, "matched_rule_ids": request.matched_rule_ids,
                "risk": request.risk, "normalized_targets": request.normalized_targets,
                "summary": request.summary, "preview_id": request.preview_id,
            }))
        with self.store.transaction():
            self.store.put("approval", record.approval_id, record.model_dump(mode="json"), execution.run_id)
            execution.update(status=RunStatus.WAITING_APPROVAL, event="approval_requested",
                             payload={"approval_id": record.approval_id})
        return record

    def resolve(self, approval_id: str, decision: ApprovalDecision) -> ApprovalRecord:
        with self.store.transaction():
            value = self.store.get("approval", approval_id)
            if value is None:
                raise RuntimeConflictError("unknown approval")
            record = ApprovalRecord.model_validate(value)
            if record.run_id != self.execution.run_id or record.status != A.PENDING:
                raise RuntimeConflictError("approval is stale or already resolved")
            if self.execution.run.status not in {RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED}:
                raise RuntimeConflictError("run is not awaiting approval")
            if record.expires_at <= now():
                raise RuntimeConflictError("approval expired")
            if (record.binding_digest != self.execution.validate_proposal()
                    or record.policy_fingerprint != self.execution.policy_fingerprint()):
                raise RuntimeConflictError("proposal or policy changed; resume to re-evaluate")
            record.decision = decision.value
            self._save(record, A.DENIED if decision == ApprovalDecision.DENY else A.APPROVED)
            self.execution.update(event="approval_resolved", payload={
                "approval_id": approval_id, "decision": decision.value})
            return record

    def consume(self) -> None:
        for value in self.store.values("approval", self.execution.run_id):
            record = ApprovalRecord.model_validate(value)
            if record.proposal_id == self.execution.run.proposal_id and record.status == A.APPROVED:
                if (record.expires_at <= now() or record.binding_digest != self.execution.validate_proposal()
                        or record.policy_fingerprint != self.execution.policy_fingerprint()):
                    raise RuntimeConflictError("approved proposal is no longer valid")
                self._save(record, A.CONSUMED)

    def invalidate_pending(self) -> None:
        for value in self.store.values("approval", self.execution.run_id):
            record = ApprovalRecord.model_validate(value)
            if record.status in {A.PENDING, A.APPROVED}:
                self._save(record, A.INVALIDATED)

    def _save(self, record, status):
        record.status = status
        record.revision += 1
        with self.store.transaction():
            self.store.put("approval", record.approval_id, record.model_dump(mode="json"), self.execution.run_id)
            self.execution.update(event=f"approval_{status.value}", payload={
                "approval_id": record.approval_id, "proposal_id": record.proposal_id})


class DurableApprovalHandler(ApprovalHandler):
    """Transport-neutral SDK/CLI adapter; decisions are not tied to a Future."""
    def __init__(self, execution, delegate=None):
        self.execution, self.delegate = execution, delegate

    async def request(self, request):
        record = self.execution.approvals.request(request)
        if record.status == A.APPROVED:
            decision = ApprovalDecision(record.decision)
        elif self.delegate is None:
            raise ExecutionPaused()
        else:
            tracker = self.execution.budget_tracker
            if tracker is not None:
                tracker.pause_active_clock()
            try:
                decision = await self.delegate.request(request)
            finally:
                if tracker is not None:
                    tracker.resume_active_clock()
            self.execution.assert_current_policy()
            self.execution.approvals.resolve(record.approval_id, decision)
        self.execution.update(status=RunStatus.RUNNING, event="approval_wait_completed")
        return decision
