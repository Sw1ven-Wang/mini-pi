"""Named fault points for deterministic recovery tests."""

from __future__ import annotations

from collections.abc import Mapping


class FaultInjectedError(RuntimeError):
    def __init__(self, point: str) -> None:
        self.point = point
        super().__init__(f"injected fault: {point}")


class NoopFaultInjector:
    def maybe_fail(self, point: str) -> None:
        return None


class ScriptedFaultInjector:
    """Raises on configured invocation numbers, counted per point."""

    def __init__(self, failures: Mapping[str, int | BaseException]) -> None:
        self.failures = dict(failures)
        self.counts: dict[str, int] = {}

    def maybe_fail(self, point: str) -> None:
        count = self.counts.get(point, 0) + 1
        self.counts[point] = count
        configured = self.failures.get(point)
        if configured is None:
            return
        if isinstance(configured, BaseException):
            raise configured
        if count == configured:
            raise FaultInjectedError(point)
