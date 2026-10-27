"""Conservative persisted reservations: unknown calls are charged, never replayed free."""

from time import monotonic
from datetime import datetime, timezone

from coding_agent.task_budget import BudgetTracker, TaskBudget


class DurableBudgetTracker(BudgetTracker):
    def __init__(self, budget: TaskBudget, store, run_id: str) -> None:
        super().__init__(budget)
        self.store, self.run_id = store, run_id
        saved = store.get_run(run_id).budget
        for name in ("turns", "model_calls", "tool_calls", "replans"):
            setattr(self, name, int(saved.get(name, 0)))
        self.spent_seconds = float(saved.get("active_seconds", 0))
        if saved.get("in_flight"):
            # Charge the unknown interval conservatively, including downtime.
            # It is never silently returned as fresh execution budget.
            elapsed = max(0, datetime.now(timezone.utc).timestamp() - float(saved["checkpoint_utc"]))
            self.spent_seconds += min(elapsed, max(0, budget.max_duration_seconds - self.spent_seconds))
        self._clock = monotonic()
        self._waiting = False
        self._in_flight = False
        limits = self.store.get("budget_limits", run_id)
        if limits is not None and limits != budget.model_dump(mode="json"):
            from coding_agent.runtime.models import RuntimeConflictError
            raise RuntimeConflictError("budget limits cannot change silently on resume")
        self.store.put("budget_limits", run_id, budget.model_dump(mode="json"), run_id)

    @property
    def remaining_seconds(self) -> float:
        elapsed = 0 if self._waiting else monotonic() - self._clock
        return self.budget.max_duration_seconds - self.spent_seconds - elapsed

    def save(self) -> None:
        current = monotonic()
        if not self._waiting:
            self.spent_seconds += current - self._clock
        self._clock = current
        run = self.store.get_run(self.run_id)
        self.store.update(run.run_id, run.revision, budget={
            **{name: getattr(self, name) for name in ("turns", "model_calls", "tool_calls", "replans")},
            "active_seconds": self.spent_seconds,
            "in_flight": int(self._in_flight),
            "checkpoint_utc": datetime.now(timezone.utc).timestamp(),
        }, event="budget_reserved")

    def _consume(self, name, current, count, limit):
        result = super()._consume(name, current, count, limit)
        setattr(self, name, result)
        self.save()
        return result

    def pause_active_clock(self):
        self.save()
        self._waiting = True

    def resume_active_clock(self):
        self._clock = monotonic()
        self._waiting = False
        self.save()

    async def run(self, awaitable):
        self._in_flight = True
        self.save()
        try:
            return await super().run(awaitable)
        finally:
            self._in_flight = False
            self.save()
