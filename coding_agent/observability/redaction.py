"""Central best-effort secret redaction for observable metadata."""

from __future__ import annotations

import re
from typing import Any

from coding_agent.observability.events import AgentEvent


REDACTED = "[REDACTED]"
_SECRET_KEYS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "secret",
    "openai_api_key",
    "deepseek_api_key",
)
_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{4,}", re.IGNORECASE),
    re.compile(
        r"\b(?:OPENAI_API_KEY|DEEPSEEK_API_KEY|[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD))\s*=\s*[^\s;]+",
        re.IGNORECASE,
    ),
)


def redact(value: Any, *, key: str = "") -> Any:
    if _is_secret_key(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(item_key): redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def redact_text(value: str) -> str:
    result = value
    for pattern in _TEXT_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def redact_event(event: AgentEvent) -> AgentEvent:
    return event.model_copy(update={"payload": redact(event.payload)}, deep=True)


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SECRET_KEYS)
