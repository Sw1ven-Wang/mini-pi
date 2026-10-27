from pathlib import Path

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import powershell_config
from coding_agent.sandbox.base import SandboxLevel
from coding_agent.sandbox.local import LocalSandbox


async def test_local_sandbox_is_explicitly_l1_not_strong(tmp_path: Path):
    session = await LocalSandbox().start(workspace=Workspace(tmp_path))
    assert session.resource_policy.level == SandboxLevel.L1_LOCAL
    assert session.resource_policy.strong_isolation is False
    assert session.resource_policy.network_isolated is False
    await session.stop()


async def test_local_sandbox_fixes_workspace_cwd(tmp_path: Path):
    session = await LocalSandbox(
        process_runner=ProcessRunner(), shell_config=powershell_config()
    ).start(workspace=Workspace(tmp_path))
    result = await session.run("(Get-Location).Path")
    await session.stop()
    assert str(tmp_path.resolve()).lower() in result.stdout.strip().lower()


async def test_local_sandbox_keeps_bounded_output(tmp_path: Path):
    session = await LocalSandbox().start(workspace=Workspace(tmp_path))
    result = await session.run(
        "1..1000 | ForEach-Object { 'x' * 100 }",
        max_output_bytes=1000,
    )
    await session.stop()
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) < 1200
