"""Host-owned, read-only reconciliation assessment; never a force-resume switch."""

import os

from coding_agent.runtime.models import RunStatus
from coding_agent.runtime.store import digest


def resources_stopped(execution) -> tuple[bool, list[dict]]:
    resources = []
    for row in execution.store.values("process_lease", execution.run_id):
        stopped = row.get("state") == "stopped" and row.get("ownership_protocol") == 1
        if (not stopped and row.get("ownership_protocol") == 1 and row.get("state") == "owned"
                and row.get("platform") == "nt" and os.name == "nt"):
            from coding_agent.process.windows_job import job_is_stopped
            stopped = bool(row.get("job_name") == "Local\\mini-pi-" + row["lease_id"]
                           and job_is_stopped(row["job_name"]))
        resources.append({"engine": "local", "state": "stopped" if stopped else "unknown",
                          "identities": [row["lease_id"]]})
    for row in execution.store.values("sandbox_lease", execution.run_id):
        resources.append({"engine": row["engine"], "state": row["state"],
            "identities": [*row["containers"], *row["networks"], *([row["proxy"]] if row["proxy"] else [])]})
    return all(row["state"] == "stopped" for row in resources), resources


def assess_recovery(execution) -> dict:
    run = execution.run
    stopped, resources = resources_stopped(execution)
    blockers = []
    paused = run.status in {RunStatus.INTERRUPTED, RunStatus.NEEDS_RECONCILIATION}
    if not paused:
        blockers.append("run_not_interrupted")
    if not stopped:
        blockers.append("resource_lifetime_unknown")
    verification = run.cursor == "verification_started"
    saved = execution.store.get("verification_context", run.run_id)
    if verification:
        if not saved or saved.get("tracking_version") != 1:
            blockers.append("legacy_verification_untracked")
        else:
            try:
                if execution.workspace_digest() != saved["source"]:
                    blockers.append("workspace_changed")
                if digest(execution.acceptance.contract().model_dump(mode="json")) != saved["contract"]:
                    blockers.append("contract_changed")
                if execution.policy_fingerprint() != saved["policy"]:
                    blockers.append("policy_changed")
            except (RuntimeError, OSError, ValueError):
                blockers.append("environment_unverifiable")
        if not all(check.replay_safe for check in execution.acceptance.contract().checks):
            blockers.append("check_not_replay_safe")
    # Ending a task preserves bytes and history. It does not assert successful
    # effects, so source/contract drift does not prevent this action.
    can_end = paused and stopped and (not verification or bool(saved and saved.get("tracking_version") == 1))
    return {"can_retry_verification": verification and not blockers,
            "can_end_preserving_files": can_end, "blockers": blockers,
            "external_resources": resources}
