"""Atomic JSON persistence for V6 sessions."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from coding_agent.errors import (
    SessionNotFoundError,
    SessionPersistenceError,
)
from coding_agent.session import (
    SessionSnapshot,
    sanitize_session_payload,
    validate_session_id,
)
from coding_agent.testing.fault_injection import NoopFaultInjector


class SessionStore:
    """Persist pure session data below a dedicated directory."""

    def __init__(self, root: Path, fault_injector=None) -> None:
        self.root = root.resolve(strict=False)
        self.fault_injector = fault_injector or NoopFaultInjector()

    def save(self, snapshot: SessionSnapshot) -> None:
        session_id = validate_session_id(snapshot.state.session_id)
        session_dir = self.root / session_id
        session_path = session_dir / "session.json"
        temp_path: str | None = None

        try:
            session_dir.mkdir(parents=True, exist_ok=True)
            # Re-validate at the persistence boundary so later in-memory
            # assignment cannot bypass metadata secret/runtime filtering.
            safe_snapshot = SessionSnapshot.model_validate(
                sanitize_session_payload(
                    snapshot.model_dump(mode="json")
                )
            )
            payload = safe_snapshot.model_dump_json(indent=2).encode(
                "utf-8"
            )
            fd, temp_path = tempfile.mkstemp(
                dir=session_dir,
                prefix=".session.",
                suffix=".tmp",
            )

            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

            self.fault_injector.maybe_fail("session_save_failure")
            os.replace(temp_path, session_path)
            temp_path = None
        except Exception as exc:
            raise SessionPersistenceError(
                f"Session 保存失败: {exc}",
                session_id=session_id,
            ) from exc
        finally:
            if temp_path is not None and os.path.exists(temp_path):
                os.remove(temp_path)

    def load(self, session_id: str) -> SessionSnapshot:
        safe_id = validate_session_id(session_id)
        session_path = self.root / safe_id / "session.json"

        if not session_path.is_file():
            raise SessionNotFoundError(session_id=safe_id)

        try:
            return SessionSnapshot.model_validate_json(
                session_path.read_bytes()
            )
        except Exception as exc:
            raise SessionPersistenceError(
                f"Session 读取失败: {exc}",
                session_id=safe_id,
            ) from exc

    def exists(self, session_id: str) -> bool:
        safe_id = validate_session_id(session_id)
        return (self.root / safe_id / "session.json").is_file()
