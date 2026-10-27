"""V6 context budget tests."""

import pytest
from pydantic import ValidationError

from ai.context import Context
from ai.messages import AssistantMessage, ToolCall, UserMessage
from coding_agent.context_budget import (
    ApproxTokenEstimator,
    ContextBudget,
    estimate_context,
)


def test_estimator_empty():
    assert ApproxTokenEstimator().estimate_text("") == 0


def test_estimator_is_deterministic_and_monotonic():
    estimator = ApproxTokenEstimator()
    small = estimator.estimate_text("hello")
    large = estimator.estimate_text("hello world, this is longer")

    assert small == estimator.estimate_text("hello")
    assert large > small
    assert estimator.estimate_text("中文") >= 2


def test_context_estimate_includes_tool_arguments():
    estimator = ApproxTokenEstimator()
    plain = Context(messages=[UserMessage(content="fix")])
    with_tool = Context(
        messages=[
            UserMessage(content="fix"),
            AssistantMessage(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read",
                        arguments={"path": "a/very/long/path.py"},
                    )
                ]
            ),
        ]
    )

    assert estimate_context(with_tool, estimator) > estimate_context(
        plain, estimator
    )


def test_budget_below_threshold():
    budget = ContextBudget(
        max_tokens=100,
        reserve_tokens=20,
        compact_threshold_ratio=0.75,
    )

    usage = budget.usage(59)

    assert usage.compact_threshold_tokens == 60
    assert usage.should_compact is False
    assert usage.available_tokens == 21


def test_budget_above_threshold():
    budget = ContextBudget(
        max_tokens=100,
        reserve_tokens=20,
        compact_threshold_ratio=0.75,
    )

    usage = budget.usage(60)

    assert usage.should_compact is True
    assert usage.exceeds_limit is False


def test_budget_reserve_tokens_and_hard_limit():
    budget = ContextBudget(max_tokens=100, reserve_tokens=20)

    assert budget.input_limit == 80
    assert budget.usage(81).exceeds_limit is True
    assert budget.usage(81).available_tokens == -1


@pytest.mark.parametrize(
    "values",
    [
        {"max_tokens": 0, "reserve_tokens": 0},
        {"max_tokens": 100, "reserve_tokens": -1},
        {"max_tokens": 100, "reserve_tokens": 100},
        {
            "max_tokens": 100,
            "reserve_tokens": 20,
            "compact_threshold_ratio": 0,
        },
        {
            "max_tokens": 100,
            "reserve_tokens": 20,
            "compact_threshold_ratio": 1,
        },
    ],
)
def test_budget_invalid_config(values):
    with pytest.raises(ValidationError):
        ContextBudget(**values)
