from __future__ import annotations

import os
import shutil

import pytest

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.shell import bash_config
from coding_agent.safety.settings import SandboxSettings
from coding_agent.sandbox.container import ContainerSandbox
from coding_agent.sandbox.container import _engine_call


pytestmark = pytest.mark.container_runtime


@pytest.mark.parametrize("engine", ["docker", "podman"])
async def test_real_l2_runtime_is_opt_in_and_workspace_scoped(
    engine, tmp_path, monkeypatch,
):
    if os.getenv("RUN_CONTAINER_SANDBOX_TESTS") != "1":
        pytest.skip("set RUN_CONTAINER_SANDBOX_TESTS=1 in container CI")
    if shutil.which(engine) is None:
        pytest.skip(f"{engine} unavailable")
    image = os.getenv("MINI_PI_L2_IMAGE")
    if not image:
        pytest.skip("set MINI_PI_L2_IMAGE to a digest-pinned Python+Bash image")

    workspace = Workspace(tmp_path)
    (tmp_path / "input.txt").write_text("inside", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("MINI_PI_TEST_SECRET", "must-not-enter-container")
    sandbox = ContainerSandbox(
        SandboxSettings(
            level="L2", engine=engine, image=image, network="none",
        ),
        shell_config=bash_config(),
    )
    session = await sandbox.start(workspace=workspace)
    try:
        result = await session.run(
            "cat input.txt; printf changed > output.txt; "
            "if test -n \"$MINI_PI_TEST_SECRET\"; then echo SECRET_LEAK; fi; "
            "if test -S /var/run/docker.sock; then echo HOST_SOCKET_VISIBLE; fi; "
            "if printf bad > /etc/mini-pi-host-write 2>/dev/null; "
            "then echo ROOTFS_WRITABLE; fi; "
            "if printf bad > .git/HEAD 2>/dev/null; then echo GIT_WRITABLE; fi; "
            "python -c \"import socket; socket.create_connection(('1.1.1.1',80),0.2)\"",
            timeout_seconds=10,
        )
    finally:
        await session.stop()
    assert "inside" in result.stdout
    assert "SECRET_LEAK" not in result.stdout
    assert "HOST_SOCKET_VISIBLE" not in result.stdout
    assert "ROOTFS_WRITABLE" not in result.stdout
    assert "GIT_WRITABLE" not in result.stdout
    assert (tmp_path / "output.txt").read_text(encoding="utf-8") == "changed"
    assert (tmp_path / ".git" / "HEAD").read_text(encoding="utf-8").startswith("ref:")
    assert result.exit_code != 0  # network=none blocks direct egress


@pytest.mark.parametrize("engine", ["docker", "podman"])
async def test_real_l2_allowlist_proxy_blocks_bypass_paths(engine, tmp_path):
    if os.getenv("RUN_CONTAINER_SANDBOX_TESTS") != "1":
        pytest.skip("set RUN_CONTAINER_SANDBOX_TESTS=1 in container CI")
    if shutil.which(engine) is None:
        pytest.skip(f"{engine} unavailable")
    image = os.getenv("MINI_PI_L2_IMAGE")
    if not image:
        pytest.skip("set MINI_PI_L2_IMAGE to a digest-pinned Python+Bash image")

    sandbox = ContainerSandbox(
        SandboxSettings(
            level="L2", engine=engine, image=image,
            network="allowlist", allowed_domains=["example.com"],
        ),
        shell_config=bash_config(),
    )
    session = await sandbox.start(workspace=Workspace(tmp_path))
    try:
        allowed = await session.run(
            "python -c \"import urllib.request; "
            "print(urllib.request.urlopen('https://example.com', timeout=5).status)\"",
            timeout_seconds=15,
        )
        unauthorized = await session.run(
            "python -c \"import urllib.request; "
            "urllib.request.urlopen('https://www.iana.org', timeout=5)\"",
            timeout_seconds=15,
        )
        direct_ip = await session.run(
            "python -c \"import socket; "
            "socket.create_connection(('1.1.1.1', 443), 2)\"",
            timeout_seconds=10,
        )
        direct_dns = await session.run(
            "python -c \"import socket; "
            "socket.create_connection(('example.com', 443), 2)\"",
            timeout_seconds=10,
        )
        dns_side_channel = await session.run(
            "python -c \"import socket; "
            "socket.getaddrinfo('www.iana.org', 443)\"",
            timeout_seconds=10,
        )
    finally:
        await session.stop()

    assert allowed.exit_code == 0
    assert unauthorized.exit_code != 0
    assert direct_ip.exit_code != 0
    assert direct_dns.exit_code != 0
    assert dns_side_channel.exit_code != 0


@pytest.mark.parametrize("engine", ["docker", "podman"])
async def test_real_l2_enforces_memory_timeout_and_cleanup(engine, tmp_path):
    if os.getenv("RUN_CONTAINER_SANDBOX_TESTS") != "1":
        pytest.skip("set RUN_CONTAINER_SANDBOX_TESTS=1 in container CI")
    if shutil.which(engine) is None:
        pytest.skip(f"{engine} unavailable")
    image = os.getenv("MINI_PI_L2_IMAGE")
    if not image:
        pytest.skip("set MINI_PI_L2_IMAGE to a digest-pinned Python+Bash image")

    sandbox = ContainerSandbox(
        SandboxSettings(
            level="L2", engine=engine, image=image, network="none",
            memory_mb=64, pids_limit=16,
        ),
        shell_config=bash_config(),
    )
    session = await sandbox.start(workspace=Workspace(tmp_path))
    try:
        memory = await session.run(
            "python -c \"x=bytearray(256*1024*1024); print(len(x))\"",
            timeout_seconds=10,
        )
        timeout = await session.run("sleep 10", timeout_seconds=0.2)
    finally:
        await session.stop()

    assert memory.exit_code != 0
    assert timeout.timed_out is True
    assert session.active_containers == set()
    proxy_code, _, _ = await _engine_call(
        engine, ["inspect", session.proxy_name], timeout=10,
    )
    internal_code, _, _ = await _engine_call(
        engine, ["network", "inspect", session.internal_network], timeout=10,
    )
    assert proxy_code != 0
    assert internal_code != 0
