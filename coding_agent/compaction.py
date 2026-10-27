"""Deterministic, tool-pair-safe V6 context compaction."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from ai.context import Context
from ai.messages import (
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
)
from coding_agent.context_budget import (
    ApproxTokenEstimator,
    TokenEstimator,
    estimate_context,
)
from coding_agent.errors import CompactionInvariantError
from coding_agent.observations import ObservationStore
from coding_agent.session import SessionState


SUMMARY_START = "<session_summary>"
SUMMARY_END = "</session_summary>"


class MessageGroup(BaseModel):
    messages: list[Message]
    estimated_tokens: int = Field(default=0, ge=0)


class CompactionSummary(BaseModel):
    user_goal: str
    completed_work: list[str] = Field(default_factory=list)
    current_state: list[str] = Field(default_factory=list)
    important_files: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    verification_state: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    plan_objective: str | None = None
    plan_constraints: list[str] = Field(default_factory=list)
    plan_acceptance_criteria: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    blocked_steps: list[str] = Field(default_factory=list)
    plan_revision: int = Field(default=0, ge=0)
    latest_replan_reason: str | None = None
    permission_mode: str = "default"
    safety_constraints: list[str] = Field(default_factory=list)


class CompactionResult(BaseModel):
    context: Context
    summary: CompactionSummary
    summary_text: str
    original_estimated_tokens: int
    compacted_estimated_tokens: int
    retained_groups: int = Field(ge=0)


def build_message_groups(
    messages: Sequence[Message],
    *,
    estimator: TokenEstimator | None = None,
) -> list[MessageGroup]:
    """Build complete atomic assistant-call/result groups."""

    token_estimator = estimator or ApproxTokenEstimator()
    groups: list[MessageGroup] = []
    index = 0

    while index < len(messages):
        message = messages[index]

        if isinstance(message, ToolResultMessage):
            raise CompactionInvariantError(
                f"存在孤立 ToolResult: {message.tool_call_id}"
            )

        if isinstance(message, AssistantMessage) and message.tool_calls:
            expected_ids = [call.id for call in message.tool_calls]

            if len(expected_ids) != len(set(expected_ids)):
                raise CompactionInvariantError(
                    "AssistantMessage 包含重复 tool_call id"
                )

            result_messages: list[Message] = []
            seen_ids: set[str] = set()

            for offset in range(1, len(expected_ids) + 1):
                result_index = index + offset

                if result_index >= len(messages):
                    raise CompactionInvariantError(
                        "Assistant tool call 缺少 ToolResult"
                    )

                result = messages[result_index]

                if not isinstance(result, ToolResultMessage):
                    raise CompactionInvariantError(
                        "Assistant tool call/result group 不完整"
                    )

                if (
                    result.tool_call_id not in expected_ids
                    or result.tool_call_id in seen_ids
                ):
                    raise CompactionInvariantError(
                        "ToolResult 与 Assistant tool calls 不匹配"
                    )

                seen_ids.add(result.tool_call_id)
                result_messages.append(result)

            if seen_ids != set(expected_ids):
                raise CompactionInvariantError(
                    "Assistant tool calls 未全部获得 ToolResult"
                )

            grouped = [message, *result_messages]
            groups.append(
                MessageGroup(
                    messages=grouped,
                    estimated_tokens=_estimate_messages(
                        grouped,
                        token_estimator,
                    ),
                )
            )
            index += len(grouped)
            continue

        groups.append(
            MessageGroup(
                messages=[message],
                estimated_tokens=_estimate_messages(
                    [message], token_estimator
                ),
            )
        )
        index += 1

    return groups


class DeterministicCompactor:
    def __init__(
        self,
        *,
        estimator: TokenEstimator | None = None,
        recent_groups: int = 4,
    ) -> None:
        if recent_groups < 0:
            raise ValueError("recent_groups 不能为负数")

        self.estimator = estimator or ApproxTokenEstimator()
        self.recent_groups = recent_groups

    def compact(
        self,
        *,
        context: Context,
        session: SessionState,
        observations: ObservationStore,
        max_recent_groups: int | None = None,
    ) -> CompactionResult:
        groups = build_message_groups(
            context.messages,
            estimator=self.estimator,
        )
        retain_count = (
            self.recent_groups
            if max_recent_groups is None
            else max_recent_groups
        )

        if retain_count < 0:
            raise ValueError("max_recent_groups 不能为负数")

        summary = self.build_summary(session, observations)
        summary_text = render_compaction_summary(summary)
        base_prompt = strip_compaction_summary(context.system_prompt)
        system_prompt = (
            f"{base_prompt.rstrip()}\n\n{summary_text}"
            if base_prompt.strip()
            else summary_text
        )

        retained_indices = set(
            range(max(0, len(groups) - retain_count), len(groups))
        )

        # The newest complete group is operational state, not disposable
        # history.  Keeping it also guarantees a final assistant answer or the
        # most recent tool result cannot disappear merely to satisfy budget.
        if groups:
            retained_indices.add(len(groups) - 1)

        goal_index = _find_goal_group(groups, session.user_goal)

        if goal_index is not None:
            retained_indices.add(goal_index)

        retained_messages: list[Message] = []

        if goal_index is None:
            retained_messages.append(
                UserMessage(content=session.user_goal)
            )

        for group_index in sorted(retained_indices):
            retained_messages.extend(groups[group_index].messages)

        compacted = Context(
            system_prompt=system_prompt,
            messages=retained_messages,
        )

        return CompactionResult(
            context=compacted,
            summary=summary,
            summary_text=summary_text,
            original_estimated_tokens=estimate_context(
                context, self.estimator
            ),
            compacted_estimated_tokens=estimate_context(
                compacted, self.estimator
            ),
            retained_groups=len(retained_indices),
        )

    def build_summary(
        self,
        session: SessionState,
        observations: ObservationStore,
    ) -> CompactionSummary:
        important_files = [
            (
                f"{observation.path}: observed ({observation.source.value})"
                if observation.exists
                else f"{observation.path}: missing ({observation.source.value})"
            )
            for observation in observations.all()
        ]

        plan = session.current_plan
        completed_steps: list[str] = []
        pending_steps: list[str] = []
        blocked_steps: list[str] = []
        if plan is not None:
            for step in plan.steps:
                label = f"{step.id}: {step.objective}"
                if step.status.value == "completed":
                    completed_steps.append(label)
                elif step.status.value in {"blocked", "failed"}:
                    blocked_steps.append(
                        label
                        + (" [failed]" if step.status.value == "failed" else "")
                    )
                else:
                    pending_steps.append(label)

        return CompactionSummary(
            user_goal=session.user_goal,
            completed_work=_metadata_list(
                session.metadata, "completed_work"
            ),
            current_state=[
                f"session status: {session.status.value}",
                f"completed model turns: {session.turn_count}",
                f"interrupted: {str(session.interrupted).lower()}",
                *_metadata_list(session.metadata, "current_state"),
            ],
            important_files=important_files,
            unresolved_issues=_metadata_list(
                session.metadata, "unresolved_issues"
            ),
            verification_state=_metadata_list(
                session.metadata, "verification_state"
            ),
            next_actions=_metadata_list(
                session.metadata, "next_actions"
            ),
            plan_objective=plan.objective if plan is not None else None,
            plan_constraints=list(plan.constraints) if plan is not None else [],
            plan_acceptance_criteria=(
                list(plan.acceptance_criteria) if plan is not None else []
            ),
            completed_steps=completed_steps,
            pending_steps=pending_steps,
            blocked_steps=blocked_steps,
            plan_revision=session.plan_revision,
            latest_replan_reason=session.latest_replan_reason,
            permission_mode=session.permission_mode.value,
            safety_constraints=[
                *session.safety_constraints.prohibited_operations,
                *( ["network forbidden"] if session.safety_constraints.network_forbidden else [] ),
                *( ["delete forbidden"] if session.safety_constraints.delete_forbidden else [] ),
                *( ["push forbidden"] if session.safety_constraints.push_forbidden else [] ),
            ],
        )


def render_compaction_summary(summary: CompactionSummary) -> str:
    sections = [
        ("Goal", [summary.user_goal]),
        ("Completed", summary.completed_work),
        ("Current state", summary.current_state),
        ("Important files", summary.important_files),
        ("Unresolved", summary.unresolved_issues),
        ("Verification", summary.verification_state),
        ("Next", summary.next_actions),
        (
            "Plan objective",
            [summary.plan_objective] if summary.plan_objective else [],
        ),
        ("Plan constraints", summary.plan_constraints),
        ("Plan acceptance criteria", summary.plan_acceptance_criteria),
        ("Plan completed steps", summary.completed_steps),
        ("Plan pending steps", summary.pending_steps),
        ("Plan blocked steps", summary.blocked_steps),
        ("Plan revision", [str(summary.plan_revision)]),
        (
            "Latest replan reason",
            [summary.latest_replan_reason]
            if summary.latest_replan_reason
            else [],
        ),
        ("Permission mode", [summary.permission_mode]),
        ("Safety constraints", summary.safety_constraints),
    ]
    lines = [SUMMARY_START]

    for title, items in sections:
        lines.extend(["", f"{title}:"])

        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- none recorded")

    lines.extend(["", SUMMARY_END])
    return "\n".join(lines)


def strip_compaction_summary(system_prompt: str) -> str:
    start = system_prompt.find(SUMMARY_START)

    if start < 0:
        return system_prompt

    end = system_prompt.find(SUMMARY_END, start)

    if end < 0:
        return system_prompt[:start].rstrip()

    end += len(SUMMARY_END)
    before = system_prompt[:start].rstrip()
    after = system_prompt[end:].strip()
    return "\n\n".join(part for part in (before, after) if part)


def _estimate_messages(
    messages: Sequence[Message],
    estimator: TokenEstimator,
) -> int:
    return sum(
        estimator.estimate_text(
            message.model_dump_json(exclude_none=True)
        )
        for message in messages
    )


def _find_goal_group(
    groups: Sequence[MessageGroup],
    user_goal: str,
) -> int | None:
    found = None

    for index, group in enumerate(groups):
        if any(
            isinstance(message, UserMessage)
            and message.content == user_goal
            for message in group.messages
        ):
            found = index

    return found


def _metadata_list(metadata: dict, key: str) -> list[str]:
    value = metadata.get(key, [])

    if isinstance(value, str):
        return [value]

    if not isinstance(value, list):
        return []

    return [str(item) for item in value]
