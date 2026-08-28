"""Tool definitions and schemas."""
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ToolDefinition(BaseModel):
    name: str

    description: str

    parameters: dict[str, Any] = Field(
        default_factory=dict
    )
