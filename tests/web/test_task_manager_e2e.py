from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage
from ai.models import Model
from ai.tools import ToolDefinition
from coding_agent.web.config import ProviderProfileConfig, ProviderRegistry, parse_allowed_root
from coding_agent.web.directories import DirectoryBrowser
from coding_agent.web.models import TaskCreateRequest, WebTaskStatus
from coding_agent.web.tasks import ActiveTaskError, TaskManager


class _FakeProvider:
    def __init__(self) -> None:
        self._responses = iter([
            {
                "objective": "Fix the value function",
                "constraints": [],
                "steps": [{
                    "id": "fix",
                    "objective": "Return the expected value",
                    "target_files": ["app.py"],
                    "evidence_queries": ["return 1"],
                    "acceptance_criteria": ["tests pass"],
                }],
                "acceptance_criteria": ["tests pass"],
                "revision": 1,
            },
            {
                "operations": [{
                    "kind": "edit_file",
                    "path": "app.py",
                    "replacements": [{"old_text": "return 1", "new_text": "return 2"}],
                }]
            },
        ])

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        yield StreamStart()
        yield StreamEnd(message=AssistantMessage(
            content=json.dumps(next(self._responses)),
        ))


class _FakeRegistry(ProviderRegistry):
    def __init__(self) -> None:
        super().__init__([ProviderProfileConfig(
            id="fake",
            label="Fake",
            base_url="https://example.test/v1",
            api_key_env="FAKE_API_KEY",
            models=["fake-model"],
            default_model="fake-model",
        )])
        self.provider = _FakeProvider()

    def create(self, profile_id: str, model_id: str):
        assert self.has(profile_id, model_id)
        return self.provider, Model(id=model_id, provider=profile_id)


async def test_task_manager_runs_fake_provider_to_diff_and_verification(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / ".mini_pi").mkdir()
    (workspace / ".mini_pi" / "settings.json").write_text(
        json.dumps({"version": 1, "permission_mode": "accept_edits"}),
        encoding="utf-8",
    )
    (workspace / "app.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8",
    )
    (workspace / "tests" / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    root = parse_allowed_root(f"Tests={tmp_path}")
    manager = TaskManager(
        directories=DirectoryBrowser([root]),
        providers=_FakeRegistry(),
        verify_command="python -m pytest -q",
    )

    created = await manager.create(TaskCreateRequest(
        root_id=root.id,
        workspace_path="workspace",
        goal="Fix the failing test",
        provider_id="fake",
        model_id="fake-model",
    ))
    for _ in range(200):
        current = manager.snapshot(created.session_id)
        if current.status in {
            WebTaskStatus.COMPLETED,
            WebTaskStatus.FAILED,
            WebTaskStatus.CANCELLED,
        }:
            break
        await asyncio.sleep(0.02)

    final = manager.snapshot(created.session_id)
    assert final.status == WebTaskStatus.COMPLETED, final.error
    assert final.result is not None and final.result.passed is True
    assert (workspace / "app.py").read_text(encoding="utf-8").endswith("return 2\n")
    preview = manager.preview(created.session_id)
    assert preview is not None
    assert "+    return 2" in preview.operations[0].diff
    assert manager.active_session_id is None
    sessions = manager.list_sessions(root.id, "workspace")
    assert sessions[0].session_id == created.session_id
    events = await manager.event_sink(created.session_id).snapshot()
    types = [event.type.value for _, event in events]
    assert "phase.changed" in types
    assert "task.completed" in types


class _SlowProvider:
    async def stream(self, model, context, tools):
        await asyncio.Event().wait()
        if False:  # pragma: no cover
            yield StreamStart()


class _SlowRegistry(_FakeRegistry):
    def create(self, profile_id: str, model_id: str):
        return _SlowProvider(), Model(id=model_id, provider=profile_id)


async def test_task_manager_enforces_single_active_task_and_cancels(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = parse_allowed_root(f"Tests={tmp_path}")
    manager = TaskManager(
        directories=DirectoryBrowser([root]),
        providers=_SlowRegistry(),
        verify_command="python -m pytest -q",
    )
    request = TaskCreateRequest(
        root_id=root.id,
        workspace_path="workspace",
        goal="Wait for cancellation",
        provider_id="fake",
        model_id="fake-model",
    )
    created = await manager.create(request)
    await asyncio.sleep(0.05)
    with pytest.raises(ActiveTaskError):
        await manager.create(request)
    cancelled = await manager.cancel(created.session_id)
    assert cancelled.status == WebTaskStatus.CANCELLED
    for _ in range(20):
        if manager.active_session_id is None:
            break
        await asyncio.sleep(0.01)
    assert manager.active_session_id is None


async def test_cancel_before_first_step_releases_slot_and_closes_stream(tmp_path):
    root = parse_allowed_root(f"Tests={tmp_path}")
    manager = TaskManager(directories=DirectoryBrowser([root]), providers=_SlowRegistry(), verify_command="unused")
    request = TaskCreateRequest(root_id=root.id, goal="Queued", provider_id="fake", model_id="fake-model")
    first = await manager.create(request)
    cancelled = await manager.cancel(first.session_id)
    assert cancelled.status == WebTaskStatus.CANCELLED
    assert manager.active_session_id is None
    events = [event async for _, event in manager.event_sink(first.session_id).stream()]
    assert [event.type.value for event in events] == ["task.cancelled"]
    next_task = await manager.create(request)
    await manager.cancel(next_task.session_id)
    assert manager.active_session_id is None


async def test_invalid_workspace_at_task_start_releases_slot(tmp_path):
    workspace = tmp_path / "disappearing"
    workspace.mkdir()
    root = parse_allowed_root(f"Tests={tmp_path}")
    manager = TaskManager(directories=DirectoryBrowser([root]), providers=_SlowRegistry(), verify_command="unused")
    request = TaskCreateRequest(root_id=root.id, workspace_path="disappearing", goal="Queued", provider_id="fake", model_id="fake-model")
    first = await manager.create(request)
    workspace.rmdir()
    await manager._records[first.session_id].asyncio_task
    assert manager.snapshot(first.session_id).status == WebTaskStatus.FAILED
    assert manager.active_session_id is None


async def test_web_runner_selects_l2_and_fails_closed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / ".mini_pi").mkdir(parents=True)
    (workspace / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (workspace / ".mini_pi/settings.json").write_text(json.dumps({
        "permission_mode": "accept_edits",
        "sandbox": {"level": "L2", "image": "test/image@sha256:" + "a" * 64},
    }), encoding="utf-8")
    monkeypatch.setattr("coding_agent.sandbox.container.shutil.which", lambda _: None)
    root = parse_allowed_root(f"Tests={tmp_path}")
    manager = TaskManager(directories=DirectoryBrowser([root]), providers=_FakeRegistry(), verify_command="should-not-run")
    first = await manager.create(TaskCreateRequest(root_id=root.id, workspace_path="workspace", goal="Fix", provider_id="fake", model_id="fake-model"))
    await manager._records[first.session_id].asyncio_task
    snapshot = manager.snapshot(first.session_id)
    assert snapshot.status == WebTaskStatus.FAILED
    assert manager.active_session_id is None
    assert snapshot.result is None
    assert "Docker/Podman" in snapshot.error
