"""Verification must not execute workspace code outside the selected sandbox."""

import asyncio

import pytest

from coding_agent.errors import SandboxUnavailableError
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessResult, ProcessRunner
from coding_agent.process.shell import default_shell_config
from coding_agent.safety.settings import SandboxSettings
from coding_agent.sandbox.factory import sandbox_from_settings
from coding_agent.verification.presets import pytest_verifier


class RecordingRunner:
    def __init__(self):
        self.environments = []

    async def run(self, command, **kwargs):
        self.environments.append(kwargs["env"])
        return ProcessResult(command=command, exit_code=0, stdout="ok", stderr="")


async def test_verifier_defaults_to_sanitized_l1_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_API_KEY", "sk-private-sentinel")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-aws-sentinel")
    runner = RecordingRunner()
    verifier = pytest_verifier(runner, default_shell_config(), command="test-only")
    assert (await verifier.verify(workspace=Workspace(tmp_path))).passed
    assert len(runner.environments) == 1
    assert not any("KEY" in name or "TOKEN" in name for name in runner.environments[0])
    assert "sk-private-sentinel" not in str(runner.environments)


async def test_verifier_l2_unavailable_never_calls_host_runner(monkeypatch, tmp_path):
    monkeypatch.setattr("coding_agent.sandbox.container.shutil.which", lambda _: None)
    runner = RecordingRunner()
    sandbox = sandbox_from_settings(SandboxSettings(level="L2", image="test/image@sha256:" + "a" * 64))
    verifier = pytest_verifier(runner, default_shell_config(), sandbox=sandbox)
    with pytest.raises(SandboxUnavailableError):
        await verifier.verify(workspace=Workspace(tmp_path))
    assert runner.environments == []


@pytest.mark.parametrize("cancel", [False, True])
async def test_verifier_stops_selected_sandbox_on_error_and_cancellation(cancel, tmp_path):
    class Session:
        stopped = False

        async def run(self, *args, **kwargs):
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("verification failed")

        async def stop(self):
            self.stopped = True

    class Sandbox:
        session = Session()

        async def start(self, *, workspace):
            return self.session

    sandbox = Sandbox()
    verifier = pytest_verifier(ProcessRunner(), default_shell_config(), sandbox=sandbox)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await verifier.verify(workspace=Workspace(tmp_path))
    assert sandbox.session.stopped
