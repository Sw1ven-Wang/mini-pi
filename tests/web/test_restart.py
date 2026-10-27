import json

import pytest

from agent.approvals import ApprovalDecision
from coding_agent.web.config import parse_allowed_root
from coding_agent.web.directories import DirectoryBrowser
from coding_agent.web.models import TaskCreateRequest, WebTaskStatus
from coding_agent.web.tasks import TaskManager, TaskStateError
from tests.web.test_task_manager_e2e import _FakeRegistry


def make_manager(tmp_path, registry=None):
    root = parse_allowed_root(f"Projects={tmp_path / 'projects'}")
    return TaskManager(directories=DirectoryBrowser([root]), providers=registry or _FakeRegistry(),
                       verify_command="python -m pytest -q", state_root=tmp_path / "private"), root


def workspace(tmp_path):
    path = tmp_path / "projects" / "demo"
    (path / "tests").mkdir(parents=True)
    (path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (path / "tests/test_app.py").write_text("from app import value\ndef test_value():\n    assert value() == 2\n", encoding="utf-8")
    return path


async def pending(manager, root):
    created = await manager.create(TaskCreateRequest(root_id=root.id, workspace_path="demo",
        provider_id="fake", model_id="fake-model", goal="Fix"))
    await manager._records[created.session_id].asyncio_task
    assert manager.snapshot(created.session_id).status == WebTaskStatus.AWAITING_APPROVAL
    return manager.snapshot(created.session_id)


@pytest.mark.parametrize("decision", [ApprovalDecision.APPROVE_ONCE, ApprovalDecision.APPROVE_SESSION])
async def test_pending_approval_and_preview_survive_restart(tmp_path, decision):
    path = workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    before = await pending(manager, root)
    await manager.shutdown()
    manager, _ = make_manager(tmp_path)
    try:
        restored = manager.snapshot(before.session_id)
        assert restored.run_id == before.run_id
        assert restored.pending_approval.approval_id == before.pending_approval.approval_id
        assert restored.pending_approval.preview_id != before.pending_approval.preview_id
        assert manager.preview(before.session_id).operations[0].diff
        manager.approve(before.session_id, restored.pending_approval.approval_id, decision)
        await manager._records[before.session_id].asyncio_task
        result = manager.snapshot(before.session_id)
        assert result.status == WebTaskStatus.COMPLETED, result.error
        assert result.acceptance.passed
        assert result.run_id == before.run_id
        assert "return 2" in (path / "app.py").read_text()
        with pytest.raises(TaskStateError):
            manager.approve(before.session_id, restored.pending_approval.approval_id, decision)
    finally:
        await manager.shutdown()


async def test_resume_replaces_and_closes_paused_event_transport(tmp_path):
    workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    try:
        before = await pending(manager, root)
        old_sink = manager.event_sink(before.session_id)
        await manager.resume(session_id=before.session_id, root_id=root.id, workspace_path="demo")
        assert old_sink.closed
        assert manager.event_sink(before.session_id) is not old_sink
        await manager._records[before.session_id].asyncio_task
    finally:
        await manager.shutdown()


async def test_external_edit_invalidates_recovered_approval(tmp_path):
    path = workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    before = await pending(manager, root)
    await manager.shutdown()
    (path / "app.py").write_text("human work", encoding="utf-8")
    manager, _ = make_manager(tmp_path)
    try:
        restored = manager.snapshot(before.session_id)
        assert restored.runtime_status == "needs_reconciliation"
        assert restored.pending_approval is None
        with pytest.raises(TaskStateError):
            manager.approve(before.session_id, before.pending_approval.approval_id, ApprovalDecision.APPROVE_ONCE)
        assert (path / "app.py").read_text() == "human work"
    finally:
        await manager.shutdown()


async def test_new_deny_policy_cannot_be_overridden_by_old_approval(tmp_path):
    path = workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    before = await pending(manager, root)
    await manager.shutdown()
    (path / ".mini_pi").mkdir()
    (path / ".mini_pi/settings.json").write_text(json.dumps({"rules": [
        {"id": "new-deny", "effect": "deny", "tool": "workspace_mutation"}]}), encoding="utf-8")
    manager, _ = make_manager(tmp_path)
    try:
        with pytest.raises(TaskStateError):
            manager.approve(before.session_id, before.pending_approval.approval_id, ApprovalDecision.APPROVE_ONCE)
        await manager.resume(session_id=before.session_id, root_id=root.id, workspace_path="demo")
        await manager._records[before.session_id].asyncio_task
        restored = manager.snapshot(before.session_id)
        assert restored.status == WebTaskStatus.FAILED
        assert "return 1" in (path / "app.py").read_text()
    finally:
        await manager.shutdown()


async def test_legacy_active_session_remains_blocked_on_repeated_import(tmp_path):
    workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    from ai.context import Context
    from coding_agent.session import SessionSnapshot, SessionState
    original = SessionSnapshot(state=SessionState(session_id="legacy", user_goal="old",
        metadata={"web": {"provider_id": "fake", "model_id": "fake-model"}}),
        context=Context(system_prompt="", messages=[]))
    store = manager._session_store(tmp_path / "projects/demo")
    store.save(original)
    try:
        for _ in range(2):
            with pytest.raises(TaskStateError, match="legacy interrupted"):
                await manager.resume(session_id="legacy", root_id=root.id, workspace_path="demo")
        assert store.load("legacy") == original
        assert manager._kernel(tmp_path / "projects/demo").store.runs("legacy") == []
    finally:
        await manager.shutdown()


async def test_bad_source_manifest_does_not_hide_recovery_report(tmp_path, monkeypatch):
    path = workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    from coding_agent.runtime.acceptance import default_contract, Requirement
    from coding_agent.runtime.models import RuntimeConflictError
    contract = default_contract(manager.verify_command)
    contract.requirements.append(Requirement(requirement_id="review", description="Human review", manual_confirmation=True))
    manager.acceptance_contract = contract
    before = await pending(manager, root)
    manager.approve(before.session_id, before.pending_approval.approval_id, ApprovalDecision.APPROVE_ONCE)
    await manager._records[before.session_id].asyncio_task
    assert manager.snapshot(before.session_id).runtime_status == "waiting_acceptance"
    (path / "app.py").write_text("human work", encoding="utf-8")
    try:
        with pytest.raises(RuntimeError):
            await manager.resume(session_id=before.session_id, root_id=root.id, workspace_path="demo")

        def invalid():
            raise RuntimeConflictError("linked source")

        monkeypatch.setattr(manager._records[before.session_id].execution, "workspace_digest", invalid)
        snapshot = manager.snapshot(before.session_id)
        assert snapshot.recovery_required
        assert snapshot.recovery.operations[0]["targets"] == ["app.py"]
        assert not snapshot.acceptance.passed
    finally:
        await manager.shutdown()


async def test_removing_whitelist_cannot_hide_an_unresolved_run(tmp_path):
    path = workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    await pending(manager, root)
    await manager.shutdown()
    other = tmp_path / "other"
    other.mkdir()
    from coding_agent.runtime.models import RuntimeConflictError
    with pytest.raises(RuntimeConflictError, match="outside the current workspace whitelist"):
        TaskManager(directories=DirectoryBrowser([parse_allowed_root(f"Other={other}")]),
            providers=_FakeRegistry(), verify_command="python -m pytest -q", state_root=tmp_path / "private")
    assert "return 1" in (path / "app.py").read_text()
