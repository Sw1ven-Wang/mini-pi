"""One authoritative run state machine, independent of UI status projections."""

from coding_agent.runtime.models import RunStatus as S, RuntimeConflictError


TRANSITIONS = {
    S.QUEUED: {S.RUNNING, S.INTERRUPTED, S.CANCELLED, S.FAILED},
    S.RUNNING: {S.WAITING_APPROVAL, S.WAITING_ACCEPTANCE, S.INTERRUPTED,
                S.NEEDS_RECONCILIATION, S.SUCCEEDED, S.FAILED, S.CANCELLED},
    S.WAITING_APPROVAL: {S.RUNNING, S.INTERRUPTED, S.RECONCILING,
                        S.FAILED, S.CANCELLED, S.NEEDS_RECONCILIATION},
    S.WAITING_ACCEPTANCE: {S.RUNNING, S.RECONCILING, S.CANCELLED,
                          S.NEEDS_RECONCILIATION, S.FAILED},
    S.INTERRUPTED: {S.RECONCILING, S.CANCELLED},
    S.RECONCILING: {S.RUNNING, S.WAITING_APPROVAL, S.WAITING_ACCEPTANCE,
                   S.NEEDS_RECONCILIATION, S.FAILED, S.CANCELLED, S.INTERRUPTED},
    S.NEEDS_RECONCILIATION: {S.RECONCILING, S.CANCELLED},
    S.SUCCEEDED: set(), S.FAILED: set(), S.CANCELLED: set(),
}


def validate_transition(previous: S, target: S) -> None:
    if previous != target and target not in TRANSITIONS[previous]:
        raise RuntimeConflictError(f"illegal runtime transition: {previous.value} -> {target.value}")
