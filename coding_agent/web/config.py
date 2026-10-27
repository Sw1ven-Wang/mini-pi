"""Server-owned directory and provider configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai.models import Model
from ai.providers.openai import OpenAIProvider
from coding_agent.web.models import ProviderProfileView, WorkspaceRootView


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class AllowedRoot:
    id: str
    label: str
    path: Path

    def public(self) -> WorkspaceRootView:
        return WorkspaceRootView(id=self.id, label=self.label)


def parse_allowed_root(value: str) -> AllowedRoot:
    label, separator, raw_path = value.partition("=")
    if not separator:
        raw_path = value
        label = Path(value).name or "Workspace"
    label = label.strip()
    if not label or len(label) > 80:
        raise ValueError("allowed-root label must contain 1-80 characters")
    path = Path(raw_path.strip()).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"allowed root is not a directory: {path}")
    digest = hashlib.sha256(str(path).casefold().encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._") or "root"
    return AllowedRoot(id=f"{slug[:40]}-{digest}", label=label, path=path)


class ProviderProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str = Field(min_length=1, max_length=80)
    base_url: str
    api_key_env: str
    models: list[str] = Field(min_length=1, max_length=100)
    default_model: str

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _SAFE_ID.fullmatch(value) is None:
            raise ValueError("provider id is not safe")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) origin without credentials")
        return value.rstrip("/")

    @field_validator("api_key_env")
    @classmethod
    def validate_env_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", value) is None:
            raise ValueError("api_key_env must be an uppercase environment variable")
        return value

    @field_validator("models")
    @classmethod
    def validate_models(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 256 for item in cleaned):
            raise ValueError("models must contain non-empty bounded ids")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("model ids must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_default(self) -> "ProviderProfileConfig":
        if self.default_model not in self.models:
            raise ValueError("default_model must be listed in models")
        return self


class ProviderConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: list[ProviderProfileConfig] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ProviderConfigFile":
        ids = [item.id for item in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("provider ids must be unique")
        return self


class ProviderRegistry:
    def __init__(self, profiles: list[ProviderProfileConfig]) -> None:
        self._profiles = {profile.id: profile for profile in profiles}
        if not self._profiles:
            raise ValueError("at least one provider profile is required")

    @classmethod
    def from_file(cls, path: Path) -> "ProviderRegistry":
        payload = json.loads(path.read_text(encoding="utf-8"))
        config = ProviderConfigFile.model_validate(payload)
        return cls(config.providers)

    def public_profiles(self) -> list[ProviderProfileView]:
        return [
            ProviderProfileView(
                id=profile.id,
                label=profile.label,
                models=profile.models,
                default_model=profile.default_model,
                configured=bool(os.getenv(profile.api_key_env)),
            )
            for profile in self._profiles.values()
        ]

    def create(self, profile_id: str, model_id: str):
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise KeyError("unknown provider profile")
        if model_id not in profile.models:
            raise ValueError("model is not allowed by the provider profile")
        api_key = os.getenv(profile.api_key_env)
        if not api_key:
            raise RuntimeError("provider API key is not configured")
        return (
            OpenAIProvider(api_key=api_key, base_url=profile.base_url),
            Model(id=model_id, provider=profile.id),
        )

    def has(self, profile_id: str, model_id: str) -> bool:
        profile = self._profiles.get(profile_id)
        return profile is not None and model_id in profile.models
