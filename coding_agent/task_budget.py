"""Global task budget shared across V8 phases."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Awaitable, TypeVar

from pydantic import BaseModel, Field

from coding_agent.errors import BudgetExhaustedError


T = TypeVar("T")


class TaskBudget(BaseModel):
    max_turns: int = Field(default=30, gt=0)
    max_model_calls: int = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=100, gt=0)
    max_replans: int = Field(default=3, ge=0)
    max_duration_seconds: float = Field(default=300, gt=0)


class BudgetTracker:
    def __init__(self, budget: TaskBudget) -> None:
        self.budget = budget
        self.started = monotonic()
        self.turns = 0
        self.model_calls = 0
        self.tool_calls = 0
        self.replans = 0

    def consume_turn(self, count: int = 1) -> None:
        self.turns = self._consume("turns", self.turns, count, self.budget.max_turns)

    def consume_model_call(self, count: int = 1) -> None:
        self.model_calls = self._consume(
            "model_calls", self.model_calls, count, self.budget.max_model_calls
        )

    def consume_tool_call(self, count: int = 1) -> None:
        self.tool_calls = self._consume(
            "tool_calls", self.tool_calls, count, self.budget.max_tool_calls
        )

    def consume_replan(self, count: int = 1) -> None:
        self.replans = self._consume(
            "replans", self.replans, count, self.budget.max_replans
        )

    def check_duration(self) -> None:
        if self.remaining_seconds <= 0:
            raise BudgetExhaustedError(
                budget="duration_seconds",
                limit=self.budget.max_duration_seconds,
            )

    @property
    def remaining_seconds(self) -> float:
        return self.budget.max_duration_seconds - (monotonic() - self.started)

    async def run(self, awaitable: Awaitable[T]) -> T:
        self.check_duration()
        try:
            return await asyncio.wait_for(awaitable, timeout=self.remaining_seconds)
        except TimeoutError as exc:
            raise BudgetExhaustedError(
                budget="duration_seconds",
                limit=self.budget.max_duration_seconds,
            ) from exc

    @staticmethod
    def _consume(name: str, current: int, count: int, limit: int) -> int:
        if count < 0:
            raise ValueError("budget count 不能为负数")
        if current + count > limit:
            raise BudgetExhaustedError(budget=name, limit=limit)
        return current + count
