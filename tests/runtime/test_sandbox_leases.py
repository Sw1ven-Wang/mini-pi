import asyncio
from types import SimpleNamespace

import pytest

from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.event_bus import EventBus
from coding_agent.process.shell import bash_config
from coding_agent.runtime.models import RecoveryRequiredError
from coding_agent.safety.settings import SandboxSettings
from coding_agent.sandbox.container import ContainerSandboxSession


def session(tmp_path, recorder):
    return ContainerSandboxSession(engine="docker",
        settings=SandboxSettings(level="L2", image="test/image@sha256:" + "a" * 64),
        shell_config=bash_config(), workspace=Workspace(tmp_path), event_bus=EventBus(),
        trace_context=None, session_id="s", resource_recorder=recorder)


async def test_resource_identity_is_committed_before_process_spawn(tmp_path, monkeypatch):
    records = []
    value = session(tmp_path, records.append)

    async def create(*args, **kwargs):
        assert records[-1]["state"] == "running"
        assert records[-1]["containers"]
        assert "--label" in args
        stream = asyncio.StreamReader()
        stream.feed_eof()

        async def wait():
            return 0

        return SimpleNamespace(stdout=stream, stderr=stream, returncode=0, wait=wait)

    monkeypatch.setattr("coding_agent.sandbox.container.asyncio.create_subprocess_exec", create)
    await value.initialize()
    result = await value.run("echo ok")
    assert result.exit_code == 0
    await value.stop()
    assert records[-1]["state"] == "stopped"
    assert records[-1]["containers"] == []


async def test_resource_persistence_failure_prevents_spawn(tmp_path, monkeypatch):
    called = False

    def broken(_):
        raise OSError("disk full")

    async def create(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("coding_agent.sandbox.container.asyncio.create_subprocess_exec", create)
    with pytest.raises(OSError):
        await session(tmp_path, broken).run("echo ok")
    assert not called


async def test_unknown_cleanup_does_not_report_success(tmp_path, monkeypatch):
    records = []
    value = session(tmp_path, records.append)
    value.active_containers.add("owned")

    async def broken(*args, **kwargs):
        return 1, "", "engine unavailable"

    monkeypatch.setattr("coding_agent.sandbox.container._engine_call", broken)
    with pytest.raises(RecoveryRequiredError):
        await value.stop()
    assert not any(record["state"] == "stopped" for record in records)
