"""Fail-closed Docker/Podman L2 sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

from coding_agent.errors import SandboxUnavailableError
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.tracing import TraceContext
from coding_agent.process.runner import OutputAccumulator, ProcessResult
from coding_agent.process.shell import ShellConfig, ShellType, default_shell_config
from coding_agent.process.termination import terminate_process_tree
from coding_agent.safety.settings import SandboxSettings
from coding_agent.sandbox.base import ResourcePolicy, SandboxLevel


class ContainerSandbox:
    level = SandboxLevel.L2_CONTAINER

    def __init__(
        self,
        settings: SandboxSettings,
        *,
        shell_config: ShellConfig | None = None,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
        session_id: str = "sandbox",
        resource_recorder=None,
    ) -> None:
        if settings.level != "L2":
            raise ValueError("ContainerSandbox requires sandbox.level=L2")
        self.settings = settings
        self.shell_config = shell_config or default_shell_config()
        self.event_bus = event_bus or EventBus()
        self.trace_context = trace_context
        self.session_id = session_id
        self.resource_recorder = resource_recorder

    async def start(self, *, workspace: Workspace) -> "ContainerSandboxSession":
        engine = await self.preflight(workspace=workspace)
        session = ContainerSandboxSession(
            engine=engine,
            settings=self.settings,
            shell_config=self.shell_config,
            workspace=workspace,
            event_bus=self.event_bus,
            trace_context=self.trace_context,
            session_id=self.session_id,
            resource_recorder=self.resource_recorder,
        )
        try:
            await session.initialize()
        except Exception as exc:
            await session.stop()
            await self._violation("l2_start", type(exc).__name__)
            if isinstance(exc, SandboxUnavailableError):
                raise
            raise SandboxUnavailableError(
                engine=engine, reason=f"容器隔离初始化失败: {type(exc).__name__}",
            ) from exc
        return session

    async def preflight(self, *, workspace: Workspace) -> str:
        """Prove the requested engine and pinned image exist; never downgrade."""
        del workspace  # Reserved for future engine-specific mount checks.
        engine = await self._select_engine()
        assert self.settings.image is not None
        inspected = await _engine_call(
            engine, ["image", "inspect", self.settings.image], timeout=10,
        )
        if inspected[0] != 0:
            await self._violation("l2_start", "pinned image is not present")
            raise SandboxUnavailableError(
                engine=engine, reason="固定 digest 的容器镜像未安装",
            )
        return engine

    async def _select_engine(self) -> str:
        candidates = (
            [self.settings.engine]
            if self.settings.engine != "auto"
            else ["docker", "podman"]
        )
        for candidate in candidates:
            if shutil.which(candidate) is None:
                continue
            try:
                code, _, _ = await _engine_call(candidate, ["info"], timeout=10)
            except SandboxUnavailableError:
                if self.settings.engine != "auto":
                    raise
                continue
            if code == 0:
                return candidate
        await self._violation("l2_start", "container engine unavailable")
        raise SandboxUnavailableError(
            engine=self.settings.engine,
            reason="Docker/Podman 不存在或 daemon 不可访问",
        )

    async def _violation(self, kind: str, reason: str) -> None:
        if self.trace_context is None:
            return
        await self.event_bus.emit(self.trace_context.child().event(
            session_id=self.session_id,
            type=AgentEventType.SANDBOX_VIOLATION,
            payload={"kind": kind, "reason": reason, "level": "L2"},
        ))


class ContainerSandboxSession:
    def __init__(
        self,
        *,
        engine: str,
        settings: SandboxSettings,
        shell_config: ShellConfig,
        workspace: Workspace,
        event_bus: EventBus,
        trace_context: TraceContext | None,
        session_id: str,
        resource_recorder=None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.shell_config = shell_config
        self.workspace = workspace
        self.event_bus = event_bus
        self.trace_context = trace_context
        self.session_id = session_id
        self.resource_recorder = resource_recorder
        self.ownership_token = uuid4().hex
        suffix = uuid4().hex[:12]
        self.internal_network = f"mini-pi-internal-{suffix}"
        self.egress_network = f"mini-pi-egress-{suffix}"
        self.proxy_name = f"mini-pi-proxy-{suffix}"
        self.proxy_address: str | None = None
        self.active_containers: set[str] = set()
        self.stopped = False
        self.resource_policy = ResourcePolicy(
            level=SandboxLevel.L2_CONTAINER,
            network_isolated=True,
            strong_isolation=True,
            fixed_cwd="/workspace",
            engine=engine,
            image=settings.image,
            network_mode=settings.network,
            allowed_domains=settings.allowed_domains,
            root_filesystem_read_only=True,
            git_read_only=True,
            non_root=True,
        )

    async def initialize(self) -> None:
        self._record_resources("starting")
        labels = ["--label", f"mini-pi.owner={self.ownership_token}"] if self.resource_recorder else []
        if self.settings.network == "allowlist":
            await self._require(["network", "create", *labels, "--internal", self.internal_network])
            await self._require(["network", "create", *labels, self.egress_network])
            proxy_script = Path(__file__).with_name("egress_proxy.py").resolve()
            mount = f"type=bind,source={proxy_script},target=/opt/mini-pi/egress_proxy.py,readonly"
            await self._require([
                "run", "-d", "--rm", "--name", self.proxy_name,
                *labels,
                "--network", self.internal_network,
                "--network-alias", "mini-pi-proxy",
                "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--user", _container_user(),
                "--pids-limit", "64", "--memory", "128m", "--cpus", "0.25",
                "--mount", mount,
                "-e", "MINI_PI_ALLOWED_DOMAINS=" + ",".join(self.settings.allowed_domains),
                self.settings.image or "",
                "python", "/opt/mini-pi/egress_proxy.py", "--port", "8080",
            ])
            await self._require([
                "network", "connect", self.egress_network, self.proxy_name,
            ])
            self.proxy_address = await self._proxy_internal_address()
            await self._wait_for_proxy()
        self._record_resources("ready")
        await self._emit(AgentEventType.SANDBOX_STARTED)

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float = 30,
        max_output_bytes: int = 100_000,
    ) -> ProcessResult:
        if self.stopped:
            raise RuntimeError("sandbox session 已停止")
        container_name = f"mini-pi-run-{uuid4().hex[:12]}"
        self.active_containers.add(container_name)
        self._record_resources("running")
        args = self._run_args(container_name, command)
        stdout_acc = OutputAccumulator(max_output_bytes)
        stderr_acc = OutputAccumulator(max_output_bytes)
        timed_out = False
        completed = False
        try:
            process = await asyncio.create_subprocess_exec(
                self.engine, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None

            async def drain(stream, accumulator) -> None:
                while chunk := await stream.read(8192):
                    accumulator.feed(chunk)

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        drain(process.stdout, stdout_acc),
                        drain(process.stderr, stderr_acc),
                    ),
                    timeout=timeout_seconds,
                )
                await process.wait()
                completed = True
            except asyncio.TimeoutError:
                timed_out = True
                await self._force_remove(container_name)
                await terminate_process_tree(process)
                with contextlib.suppress(Exception):
                    await process.wait()
                completed = True
            except asyncio.CancelledError:
                await self._force_remove(container_name)
                await terminate_process_tree(process)
                with contextlib.suppress(Exception):
                    await process.wait()
                completed = True
                raise
            stdout, out_truncated = stdout_acc.result()
            stderr, err_truncated = stderr_acc.result()
            return ProcessResult(
                command=command,
                exit_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                truncated=out_truncated or err_truncated,
            )
        finally:
            if completed or self.resource_recorder is None:
                self.active_containers.discard(container_name)
            self._record_resources("ready" if completed else "uncertain")

    def _run_args(self, name: str, command: str) -> list[str]:
        workspace_mount = (
            f"type=bind,source={self.workspace.root.resolve()},target=/workspace"
        )
        args = [
            "run", "--rm", "--name", name,
            "--workdir", "/workspace",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", _container_user(),
            "--pids-limit", str(self.settings.pids_limit),
            "--memory", f"{self.settings.memory_mb}m",
            "--cpus", str(self.settings.cpus),
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
            "--mount", workspace_mount,
        ]
        if self.resource_recorder is not None:
            args.extend(["--label", f"mini-pi.owner={self.ownership_token}"])
        git_directory = self.workspace.root / ".git"
        if git_directory.exists():
            args.extend([
                "--mount",
                f"type=bind,source={git_directory.resolve()},target=/workspace/.git,readonly",
            ])
        if self.settings.network == "none":
            args.extend(["--network", "none"])
        else:
            if self.proxy_address is None:
                raise SandboxUnavailableError(
                    engine=self.engine,
                    reason="allowlist proxy internal address is unavailable",
                )
            args.extend([
                "--network", self.internal_network,
                "--add-host", f"mini-pi-proxy:{self.proxy_address}",
                # /etc/hosts resolves the proxy name.  A loopback-only DNS
                # upstream prevents the worker from using DNS as a side
                # channel or resolving direct egress destinations.
                "--dns", "127.0.0.1",
                "-e", "HTTP_PROXY=http://mini-pi-proxy:8080",
                "-e", "HTTPS_PROXY=http://mini-pi-proxy:8080",
                "-e", "ALL_PROXY=",
                "-e", "NO_PROXY=",
            ])
        args.append(self.settings.image or "")
        if self.shell_config.shell_type == ShellType.POWERSHELL:
            args.extend(["pwsh", "-NoProfile", "-NonInteractive", "-Command", command])
        else:
            args.extend(["bash", "--noprofile", "--norc", "-c", command])
        return args

    async def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for name in list(self.active_containers):
            await self._force_remove(name)
        self.active_containers.clear()
        if self.settings.network == "allowlist":
            with contextlib.suppress(Exception):
                code, stdout, _ = await _engine_call(
                    self.engine, ["logs", self.proxy_name], timeout=10,
                )
                blocked = (
                    stdout.count("MINI_PI_NETWORK_BLOCK") if code == 0 else 0
                )
                if blocked:
                    await self._emit_violation("network", blocked)
            await self._force_remove(self.proxy_name)
            for network in (self.internal_network, self.egress_network):
                code, _, error = await _engine_call(self.engine, ["network", "rm", network], timeout=10)
                absent = network.lower() in error.lower() and (
                    "no such network" in error.lower() or "network" in error.lower() and "not found" in error.lower())
                if self.resource_recorder and code and not absent:
                    from coding_agent.runtime.models import RecoveryRequiredError
                    raise RecoveryRequiredError("cannot confirm sandbox network cleanup")
        self._record_resources("stopped")
        await self._emit(AgentEventType.SANDBOX_STOPPED)

    def _record_resources(self, state):
        if self.resource_recorder is not None:
            # Critical, synchronous persistence BEFORE external engine calls.
            self.resource_recorder({"lease_id": self.ownership_token, "engine": self.engine,
                "state": state, "containers": sorted(self.active_containers),
                "proxy": self.proxy_name if self.settings.network == "allowlist" else None,
                "networks": [self.internal_network, self.egress_network] if self.settings.network == "allowlist" else []})

    async def _emit_violation(self, kind: str, count: int = 1) -> None:
        if self.trace_context is None:
            return
        await self.event_bus.emit(self.trace_context.child().event(
            session_id=self.session_id,
            type=AgentEventType.SANDBOX_VIOLATION,
            payload={
                "kind": kind,
                "count": count,
                "level": SandboxLevel.L2_CONTAINER.value,
            },
        ))

    async def _force_remove(self, name: str) -> None:
        code, _, error = await _engine_call(self.engine, ["rm", "-f", name], timeout=10)
        absent = name.lower() in error.lower() and any(message in error.lower() for message in (
            "no such container", "no container with name", "no container with id"))
        if self.resource_recorder and code and not absent:
            from coding_agent.runtime.models import RecoveryRequiredError
            raise RecoveryRequiredError("cannot confirm container cleanup")

    async def _require(self, args: list[str]) -> None:
        code, _, stderr = await _engine_call(self.engine, args, timeout=30)
        if code != 0:
            raise SandboxUnavailableError(
                engine=self.engine,
                reason=f"container command failed: {stderr[:200]}",
            )

    async def _wait_for_proxy(self) -> None:
        probe = [
            "exec", self.proxy_name, "python", "-c",
            "import socket;s=socket.create_connection(('127.0.0.1',8080),1);s.close()",
        ]
        for _ in range(20):
            code, _, _ = await _engine_call(self.engine, probe, timeout=3)
            if code == 0:
                return
            await asyncio.sleep(0.1)
        raise SandboxUnavailableError(
            engine=self.engine, reason="allowlist proxy health check failed",
        )

    async def _proxy_internal_address(self) -> str:
        code, stdout, stderr = await _engine_call(
            self.engine, ["inspect", self.proxy_name], timeout=10,
        )
        if code != 0:
            raise SandboxUnavailableError(
                engine=self.engine,
                reason=f"cannot inspect allowlist proxy: {stderr[:200]}",
            )
        try:
            payload = json.loads(stdout)
            networks = payload[0]["NetworkSettings"]["Networks"]
            address = networks[self.internal_network]["IPAddress"]
            ipaddress.ip_address(address)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxUnavailableError(
                engine=self.engine,
                reason="allowlist proxy has no valid internal address",
            ) from exc
        return address

    async def _emit(self, event_type: AgentEventType) -> None:
        if self.trace_context is None:
            return
        await self.event_bus.emit(self.trace_context.child().event(
            session_id=self.session_id,
            type=event_type,
            payload=self.resource_policy.model_dump(mode="json"),
        ))


async def _engine_call(
    engine: str,
    args: list[str],
    *,
    timeout: float,
) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            engine, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (FileNotFoundError, OSError) as exc:
        raise SandboxUnavailableError(
            engine=engine, reason=type(exc).__name__,
        ) from exc
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(Exception):
            process.kill()
            await process.wait()
        raise SandboxUnavailableError(
            engine=engine, reason="container engine command timed out",
        ) from exc
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _container_user() -> str:
    get_uid = getattr(os, "getuid", None)
    get_gid = getattr(os, "getgid", None)
    if callable(get_uid) and callable(get_gid):
        uid, gid = get_uid(), get_gid()
        if uid > 0:
            return f"{uid}:{gid}"
    return "65532:65532"
