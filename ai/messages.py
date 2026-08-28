"""Message types exchanged with the model provider."""
from typing import Literal, TypeAlias

from pydantic import BaseModel, Field


class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"

    content: str = ""

    tool_calls: list[ToolCall] = Field(
        default_factory=list
    )


class ToolResultMessage(BaseModel):
    role: Literal["tool"] = "tool"

    tool_call_id: str

    name: str

    content: str

    is_error: bool = False


Message: TypeAlias = (
    UserMessage
    | AssistantMessage
    | ToolResultMessage
)