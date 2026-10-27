"""V6 deterministic compaction and ContextManager tests."""

from datetime import datetime, timezone

import pytest

from ai.context import Context
from ai.messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.compaction import (
    DeterministicCompactor,
    build_message_groups,
    strip_compaction_summary,
)
from coding_agent.context_budget import (
    ApproxTokenEstimator,
    ContextBudget,
    estimate_context,
)
from coding_agent.context_manager import ContextManager
from coding_agent.errors import (
    CompactionInvariantError,
    ContextBudgetExceededError,
)
from coding_agent.filesystem.versions import FileVersion
from coding_agent.observations import (
    ObservationSource,
    ObservationStore,
    ObservedFileState,
)
from coding_agent.session import SessionState


def _session(goal: str = "fix all tests") -> SessionState:
    return SessionState(
        session_id="session-1",
        user_goal=goal,
        metadata={
            "completed_work": ["inspected repository"],
            "unresolved_issues": ["test_math is still failing"],
            "verification_state": ["pytest: 1 failed"],
            "next_actions": ["repair math.py"],
        },
    )


def _observations() -> ObservationStore:
    store = ObservationStore()
    store.update(
        ObservedFileState(
            path="src/math.py",
            exists=True,
            version=FileVersion(digest="secret-hash", size_bytes=10),
            source=ObservationSource.READ,
            observed_at=datetime.now(timezone.utc),
        )
    )
    return store


def _tool_group(index: int, *, size: int = 400):
    call_id = f"call-{index}"
    return [
        AssistantMessage(
            tool_calls=[
                ToolCall(
                    id=call_id,
                    name="read",
                    arguments={"path": f"src/file_{index}.py"},
                )
            ]
        ),
        ToolResultMessage(
            tool_call_id=call_id,
            name="read",
            content="x" * size,
        ),
    ]


def _long_context() -> Context:
    messages = [UserMessage(content="fix all tests")]

    for index in range(6):
        messages.extend(_tool_group(index))

    messages.append(AssistantMessage(content="still working"))
    return Context(system_prompt="base system", messages=messages)


def test_message_group_builder_keeps_multiple_results_atomic():
    assistant = AssistantMessage(
        tool_calls=[
            ToolCall(id="a", name="read", arguments={}),
            ToolCall(id="b", name="read", arguments={}),
        ]
    )
    groups = build_message_groups(
        [
            assistant,
            ToolResultMessage(tool_call_id="b", name="read", content="b"),
            ToolResultMessage(tool_call_id="a", name="read", content="a"),
        ]
    )

    assert len(groups) == 1
    assert len(groups[0].messages) == 3


@pytest.mark.parametrize(
    "messages",
    [
        [ToolResultMessage(tool_call_id="a", name="read", content="a")],
        [
            AssistantMessage(
                tool_calls=[ToolCall(id="a", name="read", arguments={})]
            )
        ],
        [
            AssistantMessage(
                tool_calls=[ToolCall(id="a", name="read", arguments={})]
            ),
            ToolResultMessage(tool_call_id="b", name="read", content="b"),
        ],
    ],
)
def test_compaction_rejects_orphan_or_mismatched_tools(messages):
    with pytest.raises(CompactionInvariantError):
        build_message_groups(messages)


def test_compaction_reduces_context_and_preserves_system_goal():
    context = _long_context()
    estimator = ApproxTokenEstimator()
    result = DeterministicCompactor(
        estimator=estimator,
        recent_groups=2,
    ).compact(
        context=context,
        session=_session(),
        observations=_observations(),
    )

    assert result.compacted_estimated_tokens < result.original_estimated_tokens
    assert result.context.system_prompt.startswith("base system")
    assert "fix all tests" in result.context.system_prompt
    assert any(
        isinstance(message, UserMessage)
        and message.content == "fix all tests"
        for message in result.context.messages
    )


def test_compaction_keeps_retained_tool_call_and_result_together():
    context = Context(
        messages=[
            UserMessage(content="fix all tests"),
            *_tool_group(1),
            *_tool_group(2),
        ]
    )
    result = DeterministicCompactor(recent_groups=1).compact(
        context=context,
        session=_session(),
        observations=ObservationStore(),
    )

    retained = result.context.messages
    assistant = next(
        message
        for message in retained
        if isinstance(message, AssistantMessage) and message.tool_calls
    )
    result_message = next(
        message for message in retained if isinstance(message, ToolResultMessage)
    )
    assert assistant.tool_calls[0].id == result_message.tool_call_id


def test_compaction_always_preserves_latest_complete_group():
    latest = AssistantMessage(content="latest operational state")
    context = Context(
        messages=[
            UserMessage(content="fix all tests"),
            *_tool_group(1),
            latest,
        ]
    )

    result = DeterministicCompactor(recent_groups=0).compact(
        context=context,
        session=_session(),
        observations=ObservationStore(),
    )

    assert result.context.messages[-1] == latest


def test_compaction_preserves_unresolved_verification_and_file_state():
    store = _observations()
    result = DeterministicCompactor(recent_groups=0).compact(
        context=_long_context(),
        session=_session(),
        observations=store,
    )

    prompt = result.context.system_prompt
    assert "test_math is still failing" in prompt
    assert "pytest: 1 failed" in prompt
    assert "src/math.py: observed" in prompt
    assert "secret-hash" not in prompt
    assert store.get("src/math.py").version.digest == "secret-hash"  # type: ignore[union-attr]


def test_compaction_is_deterministic():
    compactor = DeterministicCompactor(recent_groups=2)
    first = compactor.compact(
        context=_long_context(),
        session=_session(),
        observations=_observations(),
    )
    second = compactor.compact(
        context=_long_context(),
        session=_session(),
        observations=_observations(),
    )

    assert first.context == second.context
    assert first.summary == second.summary


def test_strip_summary_preserves_runtime_warning_after_summary():
    prompt = (
        "base\n\n<session_summary>\nold\n</session_summary>"
        "\n\n<interrupted_session>inspect first</interrupted_session>"
    )

    stripped = strip_compaction_summary(prompt)

    assert stripped.startswith("base")
    assert "session_summary" not in stripped
    assert "interrupted_session" in stripped


def test_context_manager_below_budget_returns_same_context():
    context = Context(messages=[UserMessage(content="small")])
    manager = ContextManager(
        budget=ContextBudget(max_tokens=1000, reserve_tokens=100)
    )

    prepared = manager.prepare(
        context=context,
        session=_session("small"),
        observations=ObservationStore(),
    )

    assert prepared is context


def test_context_manager_compacts_above_threshold():
    session = _session()
    context = _long_context()
    estimator = ApproxTokenEstimator()
    manager = ContextManager(
        budget=ContextBudget(
            max_tokens=1000,
            reserve_tokens=100,
            compact_threshold_ratio=0.6,
        ),
        estimator=estimator,
        compactor=DeterministicCompactor(
            estimator=estimator,
            recent_groups=3,
        ),
    )

    prepared = manager.prepare(
        context=context,
        session=session,
        observations=_observations(),
    )

    assert estimate_context(prepared, estimator) < estimate_context(
        context, estimator
    )
    assert session.compaction_count == 1
    assert session.compacted_summary is not None


def test_context_manager_raises_if_mandatory_goal_exceeds_limit():
    goal = "x" * 2000
    context = Context(messages=[UserMessage(content=goal)])
    manager = ContextManager(
        budget=ContextBudget(
            max_tokens=100,
            reserve_tokens=20,
            compact_threshold_ratio=0.5,
        ),
        compactor=DeterministicCompactor(recent_groups=0),
    )

    with pytest.raises(ContextBudgetExceededError):
        manager.prepare(
            context=context,
            session=_session(goal),
            observations=ObservationStore(),
        )
