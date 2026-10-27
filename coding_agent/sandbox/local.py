"""L1 local constrained process runtime. It is not a strong security sandbox."""

from __future__ import annotations

import os
from collections.abc import Mapping

from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.tracing import TraceContext
from coding_agent.process.runner import ProcessResult, ProcessRunner
from coding_agent.process.shell import ShellConfig, default_shell_config
from coding_agent.sandbox.base import ResourcePolicy, SandboxLevel


DEFAULT_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_CACHE_DIR", "UV_PROJECT_ENVIRONMENT",
})
_SECRET_KEY_PARTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "CREDENTIAL")


class LocalSandbox:
    level = SandboxLevel.L1_LOCAL

    def __init__(
        self,
        *,
        process_runner: ProcessRunner | None = None,
        shell_config: ShellConfig | None = None,
        environment_allowlist: frozenset[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        event_bus: EventBus | None = None,
        trace_context: TraceContext | None = None,
        session_id: str = "sandbox",
    ) -> None:
        self.process_runner = process_runner or ProcessRunner()
        self.shell_config = shell_config or default_shell_config()
        self.environment_allowlist = environment_allowlist or DEFAULT_ENV_ALLOWLIST
        self.extra_env = dict(extra_env or {})
        self.event_bus = event_bus or EventBus()
        self.trace_context = trace_context
        self.session_id = session_id

    async def start(self, *, workspace: Workspace) -> "LocalSandboxSession":
        environment = _safe_environment(
            self.environment_allowlist,
            self.extra_env,
        )
        session = LocalSandboxSession(
            workspace=workspace,
            process_runner=self.process_runner,
            shell_config=self.shell_config,
            environment=environment,
            event_bus=self.event_bus,
            trace_context=self.trace_context,
            session_id=self.session_id,
        )
        await session._emit(AgentEventType.SANDBOX_STARTED)
        return session


class LocalSandboxSession:
    def __init__(
        self,
        *,
        workspace: Workspace,
        process_runner: ProcessRunner,
        shell_config: ShellConfig,
        environment: dict[str, str],
        event_bus: EventBus,
        trace_context: TraceContext | None,
        session_id: str,
    ) -> None:
        self.workspace = workspace
        self.process_runner = process_runner
        self.shell_config = shell_config
        self.environment = environment
        self.event_bus = event_bus
        self.trace_context = trace_context
        self.session_id = session_id
        self.stopped = False
        self.resource_policy = ResourcePolicy(
            environment_allowlist=sorted(environment),
            fixed_cwd=".",
        )

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float = 30,
        max_output_bytes: int = 100_000,
    ) -> ProcessResult:
        if self.stopped:
            raise RuntimeError("sandbox session 已停止")
        return await self.process_runner.run(
            command,
            cwd=self.workspace.root,
            shell_config=self.shell_config,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            env=self.environment,
        )

    async def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            await self._emit(AgentEventType.SANDBOX_STOPPED)

    async def _emit(self, event_type: AgentEventType) -> None:
        if self.trace_context is None:
            return
        await self.event_bus.emit(
            self.trace_context.child().event(
                session_id=self.session_id,
                type=event_type,
                payload={
                    "level": self.resource_policy.level.value,
                    "strong_isolation": False,
                    "network_isolated": False,
                },
            )
        )


def _safe_environment(
    allowlist: frozenset[str],
    extra_env: Mapping[str, str],
) -> dict[str, str]:
    allowed_upper = {key.upper() for key in allowlist}
    result: dict[str, str] = {}
    for source in (os.environ, extra_env):
        for key, value in source.items():
            upper = key.upper()
            if upper not in allowed_upper or _secret_key(upper):
                continue
            result[key] = str(value)
    return result


def _secret_key(key: str) -> bool:
    return any(part in key for part in _SECRET_KEY_PARTS)
