"""Construct the configured sandbox without silent L2 downgrade."""

from __future__ import annotations

from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import ShellConfig
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.tracing import TraceContext
from coding_agent.safety.settings import SandboxSettings
from coding_agent.sandbox.container import ContainerSandbox
from coding_agent.sandbox.local import LocalSandbox


def sandbox_from_settings(
    settings: SandboxSettings,
    *,
    process_runner: ProcessRunner | None = None,
    shell_config: ShellConfig | None = None,
    event_bus: EventBus | None = None,
    trace_context: TraceContext | None = None,
    session_id: str = "sandbox",
    resource_recorder=None,
):
    if settings.level == "L2":
        return ContainerSandbox(
            settings,
            shell_config=shell_config,
            event_bus=event_bus,
            trace_context=trace_context,
            session_id=session_id,
            resource_recorder=resource_recorder,
        )
    return LocalSandbox(
        process_runner=process_runner,
        shell_config=shell_config,
        event_bus=event_bus,
        trace_context=trace_context,
        session_id=session_id,
    )
