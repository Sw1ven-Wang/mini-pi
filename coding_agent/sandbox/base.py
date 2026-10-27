"""Sandbox protocols and resource-policy metadata."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessResult


class SandboxLevel(str, Enum):
    L0_POLICY = "L0"
    L1_LOCAL = "L1"
    L2_CONTAINER = "L2"
    L3_REMOTE = "L3"


class ResourcePolicy(BaseModel):
    level: SandboxLevel = SandboxLevel.L1_LOCAL
    network_isolated: bool = False
    strong_isolation: bool = False
    environment_allowlist: list[str] = Field(default_factory=list)
    fixed_cwd: str = "."
    process_tree_termination: bool = True
    bounded_output: bool = True
    engine: str | None = None
    image: str | None = None
    network_mode: str = "host"
    allowed_domains: list[str] = Field(default_factory=list)
    root_filesystem_read_only: bool = False
    git_read_only: bool = False
    non_root: bool = False


class SandboxSession(Protocol):
    resource_policy: ResourcePolicy

    async def run(
        self,
        command: str,
        *,
        timeout_seconds: float = 30,
        max_output_bytes: int = 100_000,
    ) -> ProcessResult: ...

    async def stop(self) -> None: ...


class Sandbox(Protocol):
    async def start(self, *, workspace: Workspace) -> SandboxSession: ...
