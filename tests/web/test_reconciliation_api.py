"""Real HTTP authorization boundary for explicit V11.1 recovery actions."""

import pytest
from contextlib import asynccontextmanager
from httpx import AsyncClient, ASGITransport

from coding_agent.filesystem.workspace import Workspace
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.safety.factory import permission_engine_from_workspace
from coding_agent.web.app import create_app
from tests.web.test_restart import make_manager, workspace


@asynccontextmanager
async def client_for(app):
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
            yield client
    finally:
        await app.state.task_manager.shutdown()


def seeded_app(tmp_path):
    path = workspace(tmp_path)
    manager, root = make_manager(tmp_path)
    kernel = RuntimeKernel(Workspace(path), state_root=tmp_path / "private")
    execution = kernel.start("session", command="python -m pytest -q", data={"web": {
        "root_id": root.id, "workspace_path": "demo", "provider_id": "fake",
        "model_id": "fake-model", "goal": "Fix"}})
    kernel.close()
    manager._discover()
    record = manager._record("session")
    execution = record.execution
    execution.permission_engine = permission_engine_from_workspace(path)
    execution.process_tracking_enabled = True
    execution.begin_verification()
    execution.kernel.mark_interrupted()
    return create_app(roots=[root], providers=manager.providers,
                      verify_command=manager.verify_command, task_manager=manager,
                      csrf_token="csrf"), path


@pytest.mark.parametrize("fault,expected", [
    ("missing_csrf", 403), ("stale", 409), ("force", 422),
    ("extra", 422), ("other_task", 404), ("unsafe_retry", 409)])
async def test_reconciliation_http_cannot_bypass_authorization(tmp_path, fault, expected):
    app, path = seeded_app(tmp_path)
    async with client_for(app) as client:
        await client.get("/api/bootstrap")
        before = (await client.get("/api/tasks/session")).json()
        payload = {"action": "end_preserving_files", "expected_revision": before["revision"]}
        headers = {"X-Mini-Pi-CSRF": "csrf"}
        target = "session"
        if fault == "missing_csrf":
            headers = {}
        elif fault == "stale":
            payload["expected_revision"] -= 1
        elif fault == "force":
            payload["action"] = "force"
        elif fault == "extra":
            payload["bypass"] = True
        elif fault == "other_task":
            target = "other"
        else:
            payload["action"] = "retry_verification"
        response = await client.post(f"/api/tasks/{target}/reconcile", json=payload, headers=headers)
        assert response.status_code == expected, response.text
        assert (await client.get("/api/tasks/session")).json()["revision"] == before["revision"]
        assert "return 1" in (path / "app.py").read_text()


async def test_end_http_is_versioned_preserves_edits_and_releases_slot(tmp_path):
    app, path = seeded_app(tmp_path)
    (path / "app.py").write_text("human changes")
    async with client_for(app) as client:
        await client.get("/api/bootstrap")
        before = (await client.get("/api/tasks/session")).json()
        payload = {"action": "end_preserving_files", "expected_revision": before["revision"]}
        response = await client.post("/api/tasks/session/reconcile", json=payload, headers={"X-Mini-Pi-CSRF": "csrf"})
        assert response.status_code == 200, response.text
        assert response.json()["runtime_status"] == "cancelled"
        assert (await client.get("/api/bootstrap")).json()["active_session_id"] is None
        assert (path / "app.py").read_text() == "human changes"
        assert (await client.post("/api/tasks/session/reconcile", json=payload,
                           headers={"X-Mini-Pi-CSRF": "csrf"})).status_code == 409
