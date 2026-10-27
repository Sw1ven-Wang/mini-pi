"""Versioned V9/V10 permission, auto-mode, and sandbox settings."""

from __future__ import annotations

import json
import ipaddress
import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "accept_edits"
    PLAN = "plan"
    AUTO = "auto"
    DONT_ASK = "dont_ask"


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    effect: PolicyEffect
    tool: str = "*"
    shell: Literal["powershell", "bash"] | None = None
    command: list[str] | None = None
    paths: list[str] = Field(default_factory=list)
    reason: str = "configured policy rule"

    @field_validator("command")
    @classmethod
    def validate_command_pattern(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value or any(not isinstance(item, str) or not item for item in value):
            raise ValueError("command pattern must contain non-empty argv tokens")
        wildcard_indexes = [index for index, item in enumerate(value) if "*" in item]
        if wildcard_indexes and (
            wildcard_indexes != [len(value) - 1] or value[-1] != "*"
        ):
            raise ValueError("only one terminal '*' command token is supported")
        return value

    @property
    def broad_shell_allow(self) -> bool:
        if self.effect != PolicyEffect.ALLOW:
            return False
        if self.command is None:
            return self.tool.lower() in {"*", "bash", "powershell"}
        if self.command[-1:] != ["*"]:
            return False
        executable = self.command[0].lower()
        return (
            len(self.command) <= 2
            or executable in {
                "python", "python3", "node", "bash", "sh", "zsh",
                "powershell", "pwsh",
            }
            or (
                executable in {"npm", "pnpm", "yarn"}
                and len(self.command) >= 2
                and self.command[1].lower() in {"run", "test", "exec", "x"}
            )
        )


class AutoModeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    model_id: str | None = None
    provider: str | None = None
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    consecutive_block_limit: int = Field(default=3, ge=1, le=20)
    total_block_limit: int = Field(default=20, ge=1, le=100)
    require_l2_for_shell: bool = True


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["L1", "L2"] = "L1"
    engine: Literal["auto", "docker", "podman"] = "auto"
    image: str | None = None
    network: Literal["none", "allowlist"] = "none"
    allowed_domains: list[str] = Field(default_factory=list)
    cpus: float = Field(default=1.0, gt=0, le=32)
    memory_mb: int = Field(default=512, ge=64, le=65536)
    pids_limit: int = Field(default=128, ge=16, le=4096)

    @field_validator("allowed_domains")
    @classmethod
    def validate_domains(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for domain in value:
            item = domain.strip().lower().rstrip(".")
            if not item or "://" in item or "/" in item or "@" in item:
                raise ValueError(f"invalid allowed domain: {domain!r}")
            candidate = item[2:] if item.startswith("*.") else item
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                pass
            else:
                raise ValueError(f"IP literals are not allowed domains: {domain!r}")
            if (
                len(candidate) > 253
                or any(
                    re.fullmatch(
                        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                        label,
                    ) is None
                    for label in candidate.split(".")
                )
            ):
                raise ValueError(f"invalid allowed domain: {domain!r}")
            normalized.append(item)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def validate_l2(self) -> "SandboxSettings":
        if self.level == "L2" and (
            self.image is None
            or re.fullmatch(r"[^\s]+@sha256:[0-9a-fA-F]{64}", self.image) is None
        ):
            raise ValueError("L2 image must be pinned with @sha256:<digest>")
        if self.network == "allowlist" and not self.allowed_domains:
            raise ValueError("allowlist network mode requires allowed_domains")
        return self


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    allow_build_test: bool = True
    rules: list[PolicyRule] = Field(default_factory=list, max_length=1000)
    auto: AutoModeSettings = Field(default_factory=AutoModeSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)


def load_security_settings(
    workspace: Path,
    *,
    session_override: SecuritySettings | dict[str, Any] | None = None,
    user_file: Path | None = None,
) -> SecuritySettings:
    """Load built-in, user, project, then session settings without weakening rules."""
    sources: list[dict[str, Any]] = []
    for path in (
        user_file or (Path.home() / ".mini_pi" / "settings.json"),
        workspace.resolve(strict=False) / ".mini_pi" / "settings.json",
    ):
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"settings must be a JSON object: {path}")
            sources.append(raw)
    if session_override is not None:
        sources.append(
            session_override.model_dump(mode="json")
            if isinstance(session_override, SecuritySettings)
            else dict(session_override)
        )

    merged: dict[str, Any] = {}
    rules_by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        source = dict(source)
        for rule in source.pop("rules", []):
            parsed = PolicyRule.model_validate(rule)
            serialized = parsed.model_dump(mode="json")
            prior = rules_by_id.get(parsed.id)
            if prior is not None and prior != serialized:
                raise ValueError(f"conflicting duplicate policy rule id: {parsed.id}")
            rules_by_id[parsed.id] = serialized
        merged = _deep_merge(merged, source)
    merged["rules"] = list(rules_by_id.values())
    return SecuritySettings.model_validate(merged or {})


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
