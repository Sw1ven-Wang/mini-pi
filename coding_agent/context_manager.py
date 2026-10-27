"""Context budget enforcement and deterministic compaction orchestration."""

from __future__ import annotations

from ai.context import Context
from coding_agent.compaction import DeterministicCompactor
from coding_agent.context_budget import (
    ApproxTokenEstimator,
    ContextBudget,
    TokenEstimator,
    estimate_context,
)
from coding_agent.errors import ContextBudgetExceededError
from coding_agent.observations import ObservationStore
from coding_agent.session import SessionState


class ContextManager:
    def __init__(
        self,
        *,
        budget: ContextBudget,
        estimator: TokenEstimator | None = None,
        compactor: DeterministicCompactor | None = None,
    ) -> None:
        self.budget = budget
        self.estimator = estimator or ApproxTokenEstimator()
        self.compactor = compactor or DeterministicCompactor(
            estimator=self.estimator
        )

    def prepare(
        self,
        *,
        context: Context,
        session: SessionState,
        observations: ObservationStore,
    ) -> Context:
        estimated = estimate_context(context, self.estimator)
        usage = self.budget.usage(estimated)

        if not usage.should_compact:
            return context

        smallest = None

        for retained in range(self.compactor.recent_groups, -1, -1):
            result = self.compactor.compact(
                context=context,
                session=session,
                observations=observations,
                max_recent_groups=retained,
            )
            smallest = result

            if (
                result.compacted_estimated_tokens
                <= self.budget.compact_threshold_tokens
            ):
                break

        assert smallest is not None

        if smallest.compacted_estimated_tokens > self.budget.input_limit:
            raise ContextBudgetExceededError(
                estimated_tokens=smallest.compacted_estimated_tokens,
                available_tokens=self.budget.input_limit,
            )

        session.compacted_summary = smallest.summary_text
        session.compaction_count += 1
        return smallest.context
