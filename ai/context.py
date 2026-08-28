"""Context assembly and window management."""
from pydantic import BaseModel, Field

from .messages import Message


class Context(BaseModel):
    system_prompt: str = ""
    messages: list[Message] = Field(
        default_factory=list
    )