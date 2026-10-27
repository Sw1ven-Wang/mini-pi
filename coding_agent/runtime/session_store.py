"""SessionStore-compatible SQLite adapter; legacy JSON import is explicit and idempotent."""

from coding_agent.errors import SessionNotFoundError
from coding_agent.session import SessionSnapshot, sanitize_session_payload, validate_session_id
from coding_agent.runtime.models import RunStep


class DurableSessionStore:
    def __init__(self, store):
        self.store = store
        self.root = store.root

    def save(self, snapshot: SessionSnapshot) -> None:
        safe = SessionSnapshot.model_validate(sanitize_session_payload(snapshot.model_dump(mode="json")))
        safe.format_version = 2
        with self.store.transaction():
            self.store.put("session", validate_session_id(safe.state.session_id), safe.model_dump(mode="json"))
            plan = safe.state.current_plan
            if plan is not None and safe.state.active_run_id is not None:
                for item in plan.steps:
                    step = RunStep(run_id=safe.state.active_run_id, step_id=item.id,
                        plan_revision=plan.revision, status=item.status.value,
                        objective=item.objective, target_files=item.target_files)
                    key = f"{step.run_id}:{step.plan_revision}:{step.step_id}"
                    self.store.put("step", key, step.model_dump(mode="json"), step.run_id)

    def load(self, session_id: str) -> SessionSnapshot:
        value = self.store.get("session", validate_session_id(session_id))
        if value is None:
            raise SessionNotFoundError(session_id=session_id)
        return SessionSnapshot.model_validate(value)

    def exists(self, session_id: str) -> bool:
        return self.store.get("session", validate_session_id(session_id)) is not None

    def import_legacy(self, snapshot: SessionSnapshot) -> SessionSnapshot:
        if not self.exists(snapshot.state.session_id):
            snapshot = snapshot.model_copy(deep=True)
            snapshot.state.metadata["v11_import"] = {
                "source_format": snapshot.format_version,
                "legacy_unverified": True,
                "requires_reconciliation": snapshot.state.status.value == "active" or snapshot.state.interrupted,
            }
            self.save(snapshot)
        return self.load(snapshot.state.session_id)
