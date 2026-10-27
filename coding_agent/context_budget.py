"""Conservative deterministic context budgeting for V6."""

from __future__ import annotations

import math
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from ai.context import Context


class TokenEstimator(Protocol):
    def estimate_text(self, text: str) -> int:
        ...


class ApproxTokenEstimator:
    """Tokenizer-free conservative estimate.

    ASCII uses roughly four characters per token.  Each non-ASCII character is
    charged one token, which intentionally compacts CJK contexts early.
    """

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0

        ascii_count = sum(ord(character) < 128 for character in text)
        non_ascii_count = len(text) - ascii_count
        return math.ceil(ascii_count / 4) + non_ascii_count


class ContextUsage(BaseModel):
    estimated_tokens: int = Field(ge=0)
    max_tokens: int = Field(gt=0)
    reserve_tokens: int = Field(ge=0)
    available_tokens: int
    compact_threshold_tokens: int = Field(gt=0)
    should_compact: bool
    exceeds_limit: bool


class ContextBudget(BaseModel):
    max_tokens: int = Field(gt=0)
    reserve_tokens: int = Field(ge=0)
    compact_threshold_ratio: float = Field(default=0.75, gt=0, lt=1)

    @model_validator(mode="after")
    def _validate_reserve(self) -> "ContextBudget":
        if self.reserve_tokens >= self.max_tokens:
            raise ValueError("reserve_tokens 必须小于 max_tokens")

        return self

    @property
    def input_limit(self) -> int:
        return self.max_tokens - self.reserve_tokens

    @property
    def compact_threshold_tokens(self) -> int:
        return max(
            1,
            int(self.input_limit * self.compact_threshold_ratio),
        )

    def usage(self, estimated_tokens: int) -> ContextUsage:
        if estimated_tokens < 0:
            raise ValueError("estimated_tokens 不能为负数")

        return ContextUsage(
            estimated_tokens=estimated_tokens,
            max_tokens=self.max_tokens,
            reserve_tokens=self.reserve_tokens,
            available_tokens=self.input_limit - estimated_tokens,
            compact_threshold_tokens=self.compact_threshold_tokens,
            should_compact=(
                estimated_tokens >= self.compact_threshold_tokens
            ),
            exceeds_limit=estimated_tokens > self.input_limit,
        )


def estimate_context(
    context: Context,
    estimator: TokenEstimator,
) -> int:
    """Estimate prompts plus structured message/tool-call payloads."""

    total = estimator.estimate_text(context.system_prompt)

    for message in context.messages:
        total += estimator.estimate_text(
            message.model_dump_json(exclude_none=True)
        )

    return total
