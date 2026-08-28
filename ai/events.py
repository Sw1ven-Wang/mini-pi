"""Event types emitted by the AI layer."""

from typing import Literal, TypeAlias

from pydantic import BaseModel

from .messages import AssistantMessage, ToolCall


class StreamStart(BaseModel):
    type: Literal["start"] = "start"


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    delta: str


class ToolCallReady(BaseModel):
    type: Literal["tool_call_ready"] = "tool_call_ready"
    tool_call: ToolCall


class StreamEnd(BaseModel):
    type: Literal["end"] = "end"
    message: AssistantMessage


class StreamError(BaseModel):
    type: Literal["error"] = "error"
    error: str


AIEvent: TypeAlias = (
    StreamStart
    | TextDelta
    | ToolCallReady
    | StreamEnd
    | StreamError
)