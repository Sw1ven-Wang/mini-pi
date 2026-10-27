from __future__ import annotations

import ssl

import pytest

from coding_agent.errors import SandboxUnavailableError
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.event_bus import EventBus
from coding_agent.process.shell import bash_config
from coding_agent.safety.settings import SandboxSettings
from coding_agent.sandbox.container import ContainerSandbox, ContainerSandboxSession
from coding_agent.sandbox.egress_proxy import (
    AllowlistProxy, _client_hello_server_name,
)


IMAGE = "example/mini-pi@sha256:" + "a" * 64


def test_l2_requires_digest_pinned_image():
    with pytest.raises(ValueError, match="pinned"):
        SandboxSettings(level="L2", image="example/mini-pi:latest")
    with pytest.raises(ValueError, match="pinned"):
        SandboxSettings(level="L2", image="example/mini-pi@sha256:not-a-digest")


def test_container_run_arguments_are_fail_closed_and_not_host_shell(tmp_path):
    settings = SandboxSettings(level="L2", image=IMAGE, network="none")
    session = ContainerSandboxSession(
        engine="docker", settings=settings, shell_config=bash_config(),
        workspace=Workspace(tmp_path), event_bus=EventBus(),
        trace_context=None, session_id="test",
    )
    command = "echo ok; rm -rf ignored-by-host"
    args = session._run_args("run-name", command)
    assert args[:2] == ["run", "--rm"]
    assert ["--network", "none"] == args[args.index("--network"):args.index("--network") + 2]
    assert "--read-only" in args
    assert ["--cap-drop", "ALL"] == args[args.index("--cap-drop"):args.index("--cap-drop") + 2]
    assert ["--pids-limit", "128"] == args[args.index("--pids-limit"):args.index("--pids-limit") + 2]
    assert ["--memory", "512m"] == args[args.index("--memory"):args.index("--memory") + 2]
    assert ["bash", "--noprofile", "--norc", "-c"] == args[-5:-1]
    assert command == args[-1]


def test_allowlist_worker_has_proxy_host_but_no_external_dns(tmp_path):
    settings = SandboxSettings(
        level="L2", image=IMAGE, network="allowlist",
        allowed_domains=["example.com"],
    )
    session = ContainerSandboxSession(
        engine="docker", settings=settings, shell_config=bash_config(),
        workspace=Workspace(tmp_path), event_bus=EventBus(),
        trace_context=None, session_id="test",
    )
    session.proxy_address = "172.28.0.2"
    args = session._run_args("run-name", "echo ok")
    assert ["--dns", "127.0.0.1"] == args[
        args.index("--dns"):args.index("--dns") + 2
    ]
    assert ["--add-host", "mini-pi-proxy:172.28.0.2"] == args[
        args.index("--add-host"):args.index("--add-host") + 2
    ]


async def test_explicit_l2_never_downgrades_when_engine_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("coding_agent.sandbox.container.shutil.which", lambda _: None)
    sandbox = ContainerSandbox(
        SandboxSettings(level="L2", image=IMAGE), shell_config=bash_config(),
    )
    with pytest.raises(SandboxUnavailableError):
        await sandbox.start(workspace=Workspace(tmp_path))


def test_egress_proxy_domain_rules_reject_ips_and_support_explicit_wildcards():
    proxy = AllowlistProxy(["example.com", "*.python.org"])
    assert proxy.allowed("example.com")
    assert proxy.allowed("docs.python.org")
    assert not proxy.allowed("python.org")
    assert not proxy.allowed("example.com.attacker.test")
    assert not proxy.allowed("127.0.0.1")
    with pytest.raises(ValueError, match="IP literals"):
        SandboxSettings(
            level="L2", image=IMAGE, network="allowlist",
            allowed_domains=["127.0.0.1"],
        )


def test_connect_gate_extracts_tls_sni_and_rejects_plain_protocols():
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(
        incoming, outgoing, server_side=False, server_hostname="example.com",
    )
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    client_hello = outgoing.read()
    assert client_hello[0] == 0x16
    record_length = int.from_bytes(client_hello[3:5], "big")
    assert _client_hello_server_name(
        client_hello[5:5 + record_length]
    ) == "example.com"
    assert _client_hello_server_name(b"GET / HTTP/1.1") is None
