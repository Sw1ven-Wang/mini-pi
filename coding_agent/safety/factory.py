"""Runtime assembly helpers for configured V9/V10 security components."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai.models import Model
from ai.providers.base import Provider
from coding_agent.safety.auto_classifier import SemanticSafetyClassifier
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.safety.settings import SecuritySettings, load_security_settings


def permission_engine_from_workspace(
    workspace: Path,
    *,
    provider: Provider | None = None,
    model: Model | None = None,
    session_override: SecuritySettings | dict[str, Any] | None = None,
    user_file: Path | None = None,
) -> CodingPermissionEngine:
    settings = load_security_settings(
        workspace,
        session_override=session_override,
        user_file=user_file,
    )
    classifier = None
    if settings.auto.enabled and provider is not None and model is not None:
        if settings.auto.provider and settings.auto.provider != model.provider:
            raise ValueError(
                "auto.provider cannot select a different provider instance; "
                "pass a matching provider and model to permission_engine_from_workspace"
            )
        classifier_model = model.model_copy(update={
            "id": settings.auto.model_id or model.id,
            "provider": model.provider,
        })
        classifier = SemanticSafetyClassifier(
            provider=provider,
            model=classifier_model,
            timeout_seconds=settings.auto.timeout_seconds,
        )
    return CodingPermissionEngine(
        settings=settings,
        auto_classifier=classifier,
    )
