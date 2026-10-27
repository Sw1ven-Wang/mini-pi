"""V11 Host runtime: one source of execution truth, independent of transport."""

from __future__ import annotations

import json
import os
import asyncio
from pathlib import Path
from uuid import uuid4

from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.mutation.models import WorkspaceMutationResult
from coding_agent.mutation.planner import plan_workspace_change_set
from coding_agent.runtime.acceptance import AcceptanceService, default_contract, workspace_fingerprint
from coding_agent.runtime.approvals import DurableApprovalService
from coding_agent.runtime.journal import MutationJournal
from coding_agent.runtime.models import (
    RunSnapshot, RunStatus, TERMINAL, RecoveryRequiredError, RuntimeConflictError,
    ExecutionPaused,
)
from coding_agent.runtime.session_store import DurableSessionStore
from coding_agent.runtime.store import RuntimeStore, digest


def workspace_identity(path: Path) -> str:
    path = path.resolve(strict=True)
    stat = path.stat()
    return digest({"path": os.path.normcase(str(path)), "device": stat.st_dev, "inode": stat.st_ino})


def default_state_root() -> Path:
    return Path.home() / ".mini_pi" / "workspaces"


class RuntimeKernel:
    def __init__(self, workspace, *, state_root: Path | None = None):
        self.workspace = workspace
        self.workspace_id = workspace_identity(workspace.root)
        root = (Path(state_root) if state_root is not None else default_state_root()) / self.workspace_id
        if root.resolve(strict=False).is_relative_to(workspace.root):
            raise RuntimeConflictError("runtime state must be outside the executable workspace")
        self.store = RuntimeStore(root)
        self.session_store = DurableSessionStore(self.store)
        self._executor_task = None

    def start(self, session_id: str, *, command: str, data: dict | None = None,
              contract=None, goal: str | None = None) -> "RuntimeExecution":
        if any(run.status not in TERMINAL for run in self.store.runs()):
            raise RuntimeConflictError("workspace has an unresolved run")
        from coding_agent.runtime.recovery import resources_stopped
        if any(not resources_stopped(self.execution(run.run_id))[0] for run in self.store.runs()):
            raise RecoveryRequiredError("a terminal run still has uncertain resources")
        with self.store.transaction():
            run = self.store.create_run(RunSnapshot(session_id=session_id, workspace_id=self.workspace_id,
                                                   data=data or {}))
            execution = RuntimeExecution(self, run.run_id)
            installed = contract or default_contract(command)
            execution.acceptance.install(installed)
            execution.update(contract_id=installed.contract_id, status=RunStatus.RUNNING)
            initial_goal = goal if goal is not None else (data or {}).get("web", {}).get("goal")
            if initial_goal is not None and not self.session_store.exists(session_id):
                from ai.context import Context
                from coding_agent.session import SessionSnapshot, SessionState
                web = (data or {}).get("web", {})
                metadata = {"web": {key: web[key] for key in ("provider_id", "model_id") if key in web}} if web else {}
                self.session_store.save(SessionSnapshot(format_version=2,
                    state=SessionState(session_id=session_id, user_goal=initial_goal,
                        active_run_id=run.run_id, metadata=metadata),
                    context=Context(system_prompt="", messages=[])))
        return execution

    def execution(self, run_id: str) -> "RuntimeExecution":
        run = self.store.get_run(run_id)
        if run.workspace_id != workspace_identity(self.workspace.root):
            raise RecoveryRequiredError("workspace identity changed")
        return RuntimeExecution(self, run_id)

    def mark_interrupted(self) -> None:
        # Startup is read-only with respect to the workspace. Never auto-execute.
        for run in self.store.runs():
            if run.status in {RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.RECONCILING}:
                self.store.update(run.run_id, run.revision, status=RunStatus.INTERRUPTED,
                                  event="process_interrupted")

    def resume(self, run_id: str, expected_revision: int | None = None) -> "RuntimeExecution":
        if self._executor_task is not None:
            raise RuntimeConflictError("workspace still has an executor")
        execution = self.execution(run_id)
        run = execution.run
        if expected_revision is not None and expected_revision != run.revision:
            raise RuntimeConflictError("stale resume revision")
        if run.status in TERMINAL:
            raise RuntimeConflictError("terminal runs require a new explicit attempt")
        if run.status in {RunStatus.RUNNING, RunStatus.QUEUED}:
            raise RuntimeConflictError("run already has an executor")
        execution.update(status=RunStatus.RECONCILING, event="recovery_started")
        try:
            from coding_agent.runtime.recovery import resources_stopped
            if not resources_stopped(execution)[0]:
                raise RecoveryRequiredError("external resource lifetime requires reconciliation")
            # Interrupted subprocesses may have external effects. No PID guessing,
            # blind retries, or claiming the old process tree has disappeared.
            if run.cursor == "verification_started":
                raise RecoveryRequiredError("interrupted verification requires process/effect reconciliation")
            if run.proposal_id:
                journal = execution.journal()
                state = journal.reconcile()
                if state == "applied":
                    paths = [path for row in journal.rows() for path in row["after"]]
                    execution.update(cursor="acceptance" if run.cursor == "acceptance" else "verify",
                        data={**execution.run.data, "changed_files": list(dict.fromkeys([*run.data.get("changed_files", []), *paths]))},
                        event="effect_reconciled")
                elif state in {"rolled_back", "abandoned"}:
                    execution.approvals.invalidate_pending()
                    execution.update(proposal_id=None, cursor="gather_evidence", event="batch_compensated")
                else:
                    execution.validate_proposal()
            execution.update(status=RunStatus.RUNNING, event="run_resumed")
            return execution
        except BaseException:
            execution.update(status=RunStatus.NEEDS_RECONCILIATION, event="recovery_blocked")
            raise

    def snapshot(self, run_id: str) -> RunSnapshot:
        return self.store.get_run(run_id)

    def reconcile(self, run_id: str, *, expected_revision: int, action: str, permission_engine=None) -> RunSnapshot:
        """Explicit CAS action, with a fresh OS/source/policy assessment at commit.

        Retry authorizes only the verification cursor; ordinary resume still
        refuses verification_started. End never compensates or writes sources.
        """
        from coding_agent.runtime.recovery import assess_recovery
        if self._executor_task is not None:
            raise RuntimeConflictError("stop the executor before reconciliation")
        execution = self.execution(run_id)
        execution.permission_engine = permission_engine
        if execution.run.revision != expected_revision:
            raise RuntimeConflictError("stale reconciliation revision")
        report = assess_recovery(execution)
        if action == "retry_verification":
            if not report["can_retry_verification"]:
                raise RecoveryRequiredError("verification retry blocked: " + ", ".join(report["blockers"]))
            with self.store.transaction():
                execution.persist_resource_proofs(report)
                execution.approvals.invalidate_pending()
                self.store.put("verification_retry", run_id,
                    self.store.get("verification_context", run_id), run_id)
                execution.update(cursor="verify", event="verification_retry_authorized")
            return execution.run
        if action == "end_preserving_files":
            if not report["can_end_preserving_files"]:
                raise RecoveryRequiredError("cannot release workspace with unknown resource lifetime")
            with self.store.transaction():
                execution.persist_resource_proofs(report)
                execution.approvals.invalidate_pending()
                result = execution.update(status=RunStatus.CANCELLED, event="recovery_ended_preserving_files")
                if self.session_store.exists(result.session_id):
                    from coding_agent.session import SessionStatus
                    snapshot = self.session_store.load(result.session_id)
                    snapshot.state.status = SessionStatus.CANCELLED
                    self.session_store.save(snapshot)
            return result
        raise ValueError("unknown reconciliation action")

    async def execute(self, execution, orchestrator, session):
        """Shared execution lifecycle for Web, CLI and SDK adapters."""
        if execution.kernel is not self or orchestrator.runtime_execution is not execution:
            raise RuntimeConflictError("executor is not bound to this kernel")
        if execution.run.status != RunStatus.RUNNING:
            raise RuntimeConflictError("run has no execution lease")
        if self._executor_task is not None:
            raise RuntimeConflictError("workspace still has an executor")
        self._executor_task = asyncio.current_task()
        try:
            return await orchestrator.run_task(session.state.user_goal, session)
        except ExecutionPaused:
            if execution.run.status not in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_ACCEPTANCE}:
                raise RuntimeConflictError("suspension has no durable wait state")
            raise
        except asyncio.CancelledError:
            execution.update(status=RunStatus.INTERRUPTED, event="executor_interrupted")
            raise
        except BaseException as exc:
            from coding_agent.errors import WorkspaceRecoveryError
            from coding_agent.runtime.recovery import resources_stopped
            if execution.run.status not in TERMINAL:
                execution.update(status=RunStatus.NEEDS_RECONCILIATION
                    if isinstance(exc, (RecoveryRequiredError, WorkspaceRecoveryError)) or not resources_stopped(execution)[0]
                    else RunStatus.FAILED,
                    event="executor_failed", payload={"error_type": type(exc).__name__})
            raise

        finally:
            self._executor_task = None

    def cancel(self, run_id: str, expected_revision: int | None = None, *, executor_stopped=False) -> RunSnapshot:
        # Keep the legacy keyword, but never accept a caller's boolean as proof.
        if self._executor_task is not None:
            raise RuntimeConflictError("stop the executor before cancelling its run")
        execution = self.execution(run_id)
        if expected_revision is not None and execution.run.revision != expected_revision:
            raise RuntimeConflictError("stale cancellation revision")
        if execution.run.status in TERMINAL:
            raise RuntimeConflictError("run is already terminal")
        from coding_agent.runtime.recovery import resources_stopped
        if not resources_stopped(execution)[0]:
            raise RecoveryRequiredError("cannot release workspace with unknown resource lifetime")
        if execution.run.cursor == "verification_started":
            context = self.store.get("verification_context", run_id)
            if not context or context.get("tracking_version") != 1 or not resources_stopped(execution)[0]:
                raise RecoveryRequiredError("cannot release workspace while old subprocess effects are uncertain")
        with self.store.transaction():
            execution.approvals.invalidate_pending()
            result = execution.update(status=RunStatus.CANCELLED, event="run_cancelled")
            if self.session_store.exists(result.session_id):
                from coding_agent.session import SessionStatus
                snapshot = self.session_store.load(result.session_id)
                snapshot.state.status = SessionStatus.CANCELLED
                self.session_store.save(snapshot)
            return result

    def close(self):
        if self._executor_task is not None and not self._executor_task.done():
            raise RuntimeConflictError("stop the executor before closing the kernel")
        if not getattr(self, "_closed", False):
            self.store.close()
            self._closed = True


class RuntimeExecution:
    def __init__(self, kernel: RuntimeKernel, run_id: str):
        self.kernel, self.store, self.run_id = kernel, kernel.store, run_id
        self.workspace = kernel.workspace
        self.permission_engine = None
        self.acceptance = AcceptanceService(self.store, run_id)
        self.approvals = DurableApprovalService(self)
        self.budget_tracker = None
        self.process_tracking_enabled = False

    @property
    def run(self):
        return self.store.get_run(self.run_id)

    def update(self, **changes):
        return self.store.update(self.run_id, self.run.revision, **changes)

    def policy_fingerprint(self) -> str:
        engine = self.permission_engine
        if engine is None:
            raise RuntimeConflictError("permission engine is not bound")
        return digest({"settings": engine.settings.model_dump(mode="json"),
                       "mode": engine.mode.value,
                       "constraints": engine.safety_constraints.model_dump(mode="json")})

    def record_sandbox_resources(self, value):
        self.store.put("sandbox_lease", value["lease_id"], value, self.run_id)
        self.update(event="sandbox_resources_recorded", payload={
            "lease_id": value["lease_id"], "state": value["state"]})

    def record_process_resources(self, value):
        with self.store.transaction():
            self.store.put("process_lease", value["lease_id"], dict(value), self.run_id)
            self.update(event="process_resources_recorded", payload={
                "lease_id": value["lease_id"], "state": value["state"]})

    def persist_resource_proofs(self, report):
        stopped = {row["identities"][0] for row in report["external_resources"]
                   if row["engine"] == "local" and row["state"] == "stopped"}
        for row in self.store.values("process_lease", self.run_id):
            if row["lease_id"] in stopped and row["state"] != "stopped":
                self.record_process_resources({**row, "state": "stopped", "proof": "os_job_empty_or_absent"})

    def assert_current_policy(self):
        from coding_agent.safety.settings import load_security_settings
        current = load_security_settings(self.workspace.root)
        if current.model_dump(mode="json") != self.permission_engine.settings.model_dump(mode="json"):
            raise RecoveryRequiredError("security settings changed during execution")

    def save_grants(self, session_id):
        self.store.put("session_grants", session_id, {"policy": self.policy_fingerprint(),
            "grants": self.permission_engine.export_session_grants()}, self.run_id)

    def restore_grants(self, session_id):
        saved = self.store.get("session_grants", session_id)
        if saved and saved["policy"] == self.policy_fingerprint():
            self.permission_engine.restore_session_grants(saved["grants"])

    def journal(self):
        proposal_id = self.run.proposal_id
        if proposal_id is None:
            raise RuntimeConflictError("run has no current proposal")
        return MutationJournal(self.store, self.workspace, self.run_id, proposal_id)

    def prepare_proposal(self, change_set: WorkspaceChangeSet) -> None:
        paths = []
        for operation in change_set.operations:
            targets = [operation.source, operation.destination] if operation.kind == "move_file" else [operation.path]
            paths.extend(self.workspace.relative(self.workspace.resolve(path)) for path in targets)
        self.acceptance.validate_mutations(paths, self.run.data.get("changed_files", []))
        planned = plan_workspace_change_set(self.workspace, change_set)
        proposal_id = str(uuid4())
        journal = MutationJournal(self.store, self.workspace, self.run_id, proposal_id)
        with self.store.transaction():
            journal.prepare(planned)
            key = self.store.write_blob(change_set.model_dump_json().encode("utf-8"))
            self.store.put("proposal", proposal_id, {"blob": key, "digest": digest(change_set.model_dump(mode="json"))}, self.run_id)
            self.update(proposal_id=proposal_id, cursor="proposal", event="proposal_prepared")

    def proposal(self) -> WorkspaceChangeSet | None:
        run = self.run
        if run.proposal_id is None:
            return None
        value = self.store.get("proposal", run.proposal_id)
        if value is None:
            raise RecoveryRequiredError("missing durable proposal")
        proposal = WorkspaceChangeSet.model_validate_json(self.store.read_blob(value["blob"]))
        if digest(proposal.model_dump(mode="json")) != value["digest"]:
            raise RecoveryRequiredError("durable proposal digest mismatch")
        return proposal

    def validate_proposal(self) -> str:
        proposal = self.proposal()
        if proposal is None:
            raise RecoveryRequiredError("no approvable proposal")
        rows = self.journal().rows()
        if not rows or any(not self.journal().matches(row["before"]) for row in rows):
            raise RecoveryRequiredError("proposal files changed; old approval cannot be reused")
        return digest({"workspace": self.kernel.workspace_id,
            "proposal": digest(proposal.model_dump(mode="json")),
            "preconditions": [row["before"] for row in rows],
            "effects": [row["after"] for row in rows],
            "contract": self.acceptance.contract().model_dump(mode="json")})

    def before_apply(self, change_set, mutation_runtime) -> None:
        if self.run.status != RunStatus.RUNNING:
            raise RuntimeConflictError("file execution requires an active run")
        self.assert_current_policy()
        self.validate_proposal()
        if digest(change_set.model_dump(mode="json")) != digest(self.proposal().model_dump(mode="json")):
            raise RecoveryRequiredError("execution differs from approved proposal")
        self.approvals.consume()
        mutation_runtime.journal = self.journal()

    def mutation_complete(self, mutation) -> None:
        # No source diffs in durable runtime state or audit events.
        safe = mutation.model_dump(mode="json")
        for item in safe["mutations"]:
            item["diff"] = ""
        self.store.put("mutation_result", self.run.proposal_id, safe, self.run_id)
        paths = list(dict.fromkeys([*self.run.data.get("changed_files", []), *mutation.changed_files]))
        self.update(cursor="verify", data={**self.run.data, "changed_files": paths}, event="mutation_committed")

    def restored_mutation(self):
        value = self.store.get("mutation_result", self.run.proposal_id)
        if value:
            return WorkspaceMutationResult.model_validate(value)
        rows = self.journal().rows()
        paths = list(dict.fromkeys(path for row in rows for path in row["after"]))
        from coding_agent.changes.models import MutationResult
        from coding_agent.filesystem.versions import version_from_bytes
        mutations = []
        for row in rows:
            path = list(row["after"])[-1]
            old_key = next(iter(row["before"].values()))
            new_key = row["after"][path]
            mutations.append(MutationResult(path=path,
                old_version=version_from_bytes(self.store.read_blob(old_key)) if old_key else None,
                new_version=version_from_bytes(self.store.read_blob(new_key)) if new_key else None))
        return WorkspaceMutationResult(mutations=mutations, changed_files=paths)

    def begin_verification(self) -> str:
        self.assert_current_policy()
        fingerprint = self.workspace_digest()
        retry = self.store.get("verification_retry", self.run_id)
        if retry:
            from coding_agent.runtime.recovery import resources_stopped
            if (retry["source"] != fingerprint or retry["policy"] != self.policy_fingerprint()
                    or retry["contract"] != digest(self.acceptance.contract().model_dump(mode="json"))
                    or not resources_stopped(self)[0]):
                raise RecoveryRequiredError("verification retry preconditions changed")
        with self.store.transaction():
            self.store.db.execute("DELETE FROM values_store WHERE kind='verification_retry' AND id=?", (self.run_id,))
            self.store.put("verification_invariants", self.run_id,
                {"digest": self.acceptance.protected_fingerprint(self.workspace)}, self.run_id)
            self.store.put("verification_context", self.run_id, {
                "tracking_version": int(self.process_tracking_enabled), "source": fingerprint, "policy": self.policy_fingerprint(),
                "contract": digest(self.acceptance.contract().model_dump(mode="json"))}, self.run_id)
            self.update(cursor="verification_started", event="verification_reserved")
        return fingerprint

    def verification_complete(self, decision, fingerprint: str):
        protected = self.store.get("verification_invariants", self.run_id)
        if not protected or protected["digest"] != self.acceptance.protected_fingerprint(self.workspace):
            raise RecoveryRequiredError("verification changed protected acceptance paths")
        after = self.workspace_digest()
        if after != fingerprint:
            raise RecoveryRequiredError("verification changed checked source; evidence invalid")
        with self.store.transaction():
            self.acceptance.record("verification_pipeline", decision.passed, fingerprint)
            self.store.put("verification_policy", self.run_id, {"digest": self.policy_fingerprint()}, self.run_id)
            self.store.put("verification_decision", self.run_id, decision.model_dump(mode="json"), self.run_id)
            self.update(cursor="acceptance", event="acceptance_evaluated")
        return self.acceptance.evaluate(fingerprint)

    def replan(self):
        self.approvals.invalidate_pending()
        if self.run.proposal_id:
            for row in self.journal().rows():
                if row["status"] == "prepared":
                    self.store.put("operation", row["operation_id"], {**row, "status": "abandoned"}, self.run_id)
        self.update(proposal_id=None, cursor="gather_evidence", event="replan_reserved")

    def workspace_digest(self):
        return workspace_fingerprint(self.workspace, include_paths=self.run.data.get("changed_files", []))

    def succeed(self, outcome):
        from coding_agent.runtime.recovery import resources_stopped
        if not resources_stopped(self)[0]:
            raise RecoveryRequiredError("unfinished external resources cannot be accepted")
        from coding_agent.runtime.models import OperationRecord
        operations = [OperationRecord.model_validate(item).model_dump(mode="json")
                      for item in self.store.values("operation", self.run_id)]
        if any(item["status"] not in {"applied", "rolled_back", "abandoned"} for item in operations):
            raise RuntimeConflictError("unfinished file operations cannot be accepted")
        if any(item["status"] in {"pending", "approved"} for item in self.store.values("approval", self.run_id)):
            raise RuntimeConflictError("unconsumed approval cannot be accepted")
        self.assert_current_policy()
        environment = self.store.get("verification_policy", self.run_id)
        if not environment or environment["digest"] != self.policy_fingerprint():
            raise RecoveryRequiredError("verification policy changed; old evidence cannot complete a run")
        acceptance = self.acceptance.evaluate(self.workspace_digest())
        if not acceptance.passed:
            raise RuntimeConflictError("acceptance contract does not permit success")
        # TaskOutcome includes read evidence; keep only the public result projection.
        value = outcome.model_dump(mode="json", exclude={"evidence"})
        self.update(status=RunStatus.SUCCEEDED, cursor="final", data={**self.run.data, "outcome": value}, event="run_succeeded")
