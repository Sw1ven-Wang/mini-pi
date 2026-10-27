import asyncio
import os
import shlex
import sys
from types import SimpleNamespace

import pytest

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.runtime.models import RecoveryRequiredError, RuntimeConflictError, RunStatus
from coding_agent.runtime.recovery import assess_recovery
from coding_agent.safety.factory import permission_engine_from_workspace


@pytest.fixture
def execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("value = 1\n")
    kernel = RuntimeKernel(Workspace(workspace), state_root=tmp_path / "private")
    execution = kernel.start("s", command="test", goal="goal")
    execution.permission_engine = permission_engine_from_workspace(workspace)
    execution.process_tracking_enabled = True
    contract = execution.acceptance.contract()
    contract.checks[0].replay_safe = True
    # Installation is host-owned, before any check/evidence exists.
    kernel.store.put("contract", execution.run_id, contract.model_dump(mode="json"), execution.run_id)
    yield execution
    kernel.close()


def interrupt(execution):
    execution.begin_verification()
    execution.kernel.mark_interrupted()


def act(execution, action="retry_verification", revision=None):
    return execution.kernel.reconcile(execution.run_id, action=action,
        permission_engine=execution.permission_engine,
        expected_revision=execution.run.revision if revision is None else revision)


def test_read_only_assessment_and_explicit_retry(execution):
    interrupt(execution)
    before = execution.run
    report = assess_recovery(execution)
    assert report["can_retry_verification"] and report["can_end_preserving_files"]
    assert execution.run == before
    with pytest.raises(RecoveryRequiredError):
        execution.kernel.resume(execution.run_id)
    act(execution)
    resumed = execution.kernel.resume(execution.run_id)
    resumed.permission_engine = execution.permission_engine
    resumed.process_tracking_enabled = True
    resumed.begin_verification()
    assert resumed.run.cursor == "verification_started"


@pytest.mark.parametrize("change,blocker", [
    ("source", "workspace_changed"), ("policy", "policy_changed"),
    ("contract", "contract_changed"), ("unsafe_check", "check_not_replay_safe"),
    ("legacy", "legacy_verification_untracked"), ("resource", "resource_lifetime_unknown")])
def test_conditions_cannot_be_overridden(execution, change, blocker):
    interrupt(execution)
    if change == "source":
        (execution.workspace.root / "app.py").write_text("human work\n")
    elif change == "policy":
        execution.permission_engine.mode = type(execution.permission_engine.mode).PLAN
    elif change in {"contract", "unsafe_check"}:
        contract = execution.acceptance.contract()
        if change == "contract":
            contract.revision += 1
        else:
            contract.checks[0].replay_safe = False
        execution.store.put("contract", execution.run_id, contract.model_dump(mode="json"), execution.run_id)
    elif change == "legacy":
        execution.store.db.execute("DELETE FROM values_store WHERE kind='verification_context'")
    else:
        execution.record_process_resources({"lease_id": "spawn-gap", "platform": os.name, "state": "reserved", "job_name": None})
    assert blocker in assess_recovery(execution)["blockers"]
    with pytest.raises(RecoveryRequiredError):
        act(execution)
    assert execution.run.status == RunStatus.INTERRUPTED


def test_end_keeps_external_edits_and_invalidates_approval(execution):
    interrupt(execution)
    path = execution.workspace.root / "app.py"
    path.write_text("human work\n")
    act(execution, "end_preserving_files")
    assert execution.run.status == RunStatus.CANCELLED
    assert path.read_text() == "human work\n"
    assert execution.kernel.session_store.load("s").state.status.value == "cancelled"
    execution.kernel.start("new", command="test")


def test_stale_duplicate_active_and_unknown_actions(execution):
    interrupt(execution)
    with pytest.raises(RuntimeConflictError):
        act(execution, revision=0)
    with pytest.raises(ValueError):
        act(execution, action="force")
    revision = execution.run.revision
    act(execution)
    with pytest.raises(RuntimeConflictError):
        act(execution, revision=revision)
    execution.kernel._executor_task = SimpleNamespace(done=lambda: False)
    try:
        with pytest.raises(RuntimeConflictError):
            act(execution)
    finally:
        execution.kernel._executor_task = None


@pytest.mark.parametrize("action", ["retry_verification", "end_preserving_files"])
def test_spawn_gap_and_legacy_cannot_release_workspace(execution, action):
    interrupt(execution)
    execution.record_process_resources({"lease_id": "gap", "state": "reserved", "platform": os.name, "job_name": None})
    with pytest.raises(RecoveryRequiredError):
        act(execution, action)
    with pytest.raises(RecoveryRequiredError):
        execution.kernel.cancel(execution.run_id, executor_stopped=True)


def test_retry_ticket_revalidates_external_edit_before_execution(execution):
    interrupt(execution)
    act(execution)
    execution = execution.kernel.resume(execution.run_id)
    execution.permission_engine = permission_engine_from_workspace(execution.workspace.root)
    (execution.workspace.root / "app.py").write_text("changed after authorization")
    with pytest.raises(RecoveryRequiredError, match="preconditions"):
        execution.begin_verification()


async def test_real_process_lease_and_no_sensitive_command(execution):
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = ("& " + quote(sys.executable) + " -c 'print(42)'" if os.name == "nt"
               else shlex.quote(sys.executable) + " -c 'print(42)'")
    result = await ProcessRunner(parent_lifetime_guard=True,
        resource_recorder=execution.record_process_resources).run(command, cwd=execution.workspace.root)
    assert result.exit_code == 0
    rows = execution.store.values("process_lease", execution.run_id)
    assert len(rows) == 1 and rows[0]["state"] == "stopped"
    assert command not in str(rows)


async def test_reservation_failure_prevents_process_spawn(execution, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("must not spawn after failed durable reservation")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    def failed(_):
        raise OSError("disk full")
    with pytest.raises(OSError):
        await ProcessRunner(resource_recorder=failed).run("echo unsafe", cwd=execution.workspace.root)


def test_custom_untracked_verifier_cannot_claim_replay_proof(execution):
    execution.process_tracking_enabled = False
    interrupt(execution)
    assert "legacy_verification_untracked" in assess_recovery(execution)["blockers"]


def test_terminal_failure_with_unknown_resources_cannot_start_new_run(execution):
    execution.record_process_resources({"lease_id": "unknown", "state": "reserved", "platform": os.name, "job_name": None})
    execution.update(status=RunStatus.FAILED)
    with pytest.raises(RecoveryRequiredError):
        execution.kernel.start("another", command="test")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership gate")
async def test_windows_command_cannot_start_before_durable_job_ownership(execution):
    target = execution.workspace.root / "must-not-exist"
    code = "from pathlib import Path; Path('must-not-exist').write_text('unsafe')"
    command = "& '" + sys.executable.replace("'", "''") + "' -c '" + code.replace("'", "''") + "'"
    def recorder(value):
        if value["state"] == "owned":
            raise OSError("ownership persistence failed")
        execution.record_process_resources(value)
    from coding_agent.errors import ProcessStartFailedError
    with pytest.raises(ProcessStartFailedError):
        await ProcessRunner(parent_lifetime_guard=True, resource_recorder=recorder).run(command, cwd=execution.workspace.root)
    assert not target.exists()


async def test_repeated_real_process_ownership_has_no_stale_os_error(execution):
    from coding_agent.errors import ProcessStartFailedError
    runner = ProcessRunner(parent_lifetime_guard=True, resource_recorder=execution.record_process_resources)
    command = "Write-Output 42" if os.name == "nt" else "printf 42"
    for _ in range(20):
        try:
            result = await runner.run(command, cwd=execution.workspace.root)
        except ProcessStartFailedError as exc:
            pytest.fail(f"owned startup failed: {exc.details}, cause={exc.__cause__!r}")
        assert result.exit_code == 0
    assert all(row["state"] == "stopped" for row in execution.store.values("process_lease", execution.run_id))


def test_legacy_stopped_boolean_cannot_override_a_live_executor(execution):
    execution.kernel._executor_task = SimpleNamespace(done=lambda: False)
    try:
        with pytest.raises(RuntimeConflictError):
            execution.kernel.cancel(execution.run_id, executor_stopped=True)
        assert execution.run.status == RunStatus.RUNNING
    finally:
        execution.kernel._executor_task = None


async def test_live_owned_job_is_not_mistaken_for_an_absent_named_job(execution):
    from coding_agent.runtime.recovery import resources_stopped
    owned = asyncio.Event()
    def recorder(value):
        execution.record_process_resources(value)
        if value["state"] == "owned":
            owned.set()
    command = ("while (-not (Test-Path finished.flag)) { Start-Sleep -Milliseconds 50 }" if os.name == "nt"
               else "while [ ! -f finished.flag ]; do sleep 0.05; done")
    task = asyncio.create_task(ProcessRunner(parent_lifetime_guard=True,
        resource_recorder=recorder).run(command, cwd=execution.workspace.root, timeout_seconds=10))
    try:
        await asyncio.wait_for(owned.wait(), 5)
        assert not resources_stopped(execution)[0]
    finally:
        (execution.workspace.root / "finished.flag").write_text("stop")
        await task
    assert resources_stopped(execution)[0]
