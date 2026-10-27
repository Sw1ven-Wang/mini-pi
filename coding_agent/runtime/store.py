"""SQLite authoritative state + transactional event outbox + private blobs.

File effects and SQLite commits are deliberately NOT a distributed transaction.
The journal reconciles that gap. All mutation callers must propagate failures.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from coding_agent.runtime.models import (
    RunSnapshot, RunStatus, RuntimeConflictError, RuntimePersistenceError, now,
)
from coding_agent.runtime.privacy import private_directory, is_link
from coding_agent.runtime.transitions import validate_transition
from coding_agent.observability.redaction import redact


# V11.1 protocol fence: old V11 writers must not interpret verification retry
# cursors without the new proof checks. Tables and Run format remain unchanged.
SCHEMA_VERSION = 2


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


class RuntimeStore:
    def __init__(self, root: Path) -> None:
        marker = Path(root) / "RESTORE_IN_PROGRESS"
        if marker.exists() or is_link(marker):
            raise RuntimePersistenceError("incomplete restore requires a new destination")
        self.root = private_directory(Path(root))
        self.blob_root = private_directory(self.root / "recovery")
        for name in ("writer.lock", "runtime.db", "runtime.db-wal", "runtime.db-shm"):
            item = self.root / name
            if is_link(item):
                raise RuntimePersistenceError("linked runtime storage file")
        self._lock_file = (self.root / "writer.lock").open("a+b")
        try:
            self._acquire_lock()
            self.db = sqlite3.connect(self.root / "runtime.db", timeout=5, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise RuntimePersistenceError("unsupported runtime schema version")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                self.db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL)")
                self.db.execute("CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id)")
                self.db.execute("CREATE TABLE IF NOT EXISTS values_store (kind TEXT NOT NULL, id TEXT NOT NULL, run_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,id))")
                self.db.execute("CREATE TABLE IF NOT EXISTS events (run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(run_id,sequence))")
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lock_file.close()
            raise

    def _acquire_lock(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self._lock_file.seek(0)
                if not self._lock_file.read(1):
                    self._lock_file.write(b"0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimePersistenceError("runtime workspace already has a writer") from exc

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        try:
            self.db.execute("SAVEPOINT runtime_nested" if nested else "BEGIN IMMEDIATE")
            yield
            self.db.execute("RELEASE runtime_nested" if nested else "COMMIT")
        except BaseException:
            if self.db.in_transaction:
                if nested:
                    self.db.execute("ROLLBACK TO runtime_nested")
                    self.db.execute("RELEASE runtime_nested")
                else:
                    self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()
        self._lock_file.close()

    def create_run(self, run: RunSnapshot) -> RunSnapshot:
        with self.transaction():
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?)", (
                run.run_id, run.session_id, run.revision, run.model_dump_json()))
            self._event(run, "run_created", {})
            self._write_run(run)
        return run.model_copy(deep=True)

    def get_run(self, run_id: str) -> RunSnapshot:
        row = self.db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        run = RunSnapshot.model_validate_json(row[0])
        if run.format_version != 1:
            raise RuntimePersistenceError("unsupported run format")
        return run

    def runs(self, session_id: str | None = None) -> list[RunSnapshot]:
        rows = self.db.execute(
            "SELECT payload FROM runs" + (" WHERE session_id=?" if session_id else ""),
            (session_id,) if session_id else (),
        ).fetchall()
        return sorted((RunSnapshot.model_validate_json(row[0]) for row in rows),
                      key=lambda item: item.created_at)

    def update(self, run_id: str, expected_revision: int, *, status: RunStatus | None = None,
               event: str = "run_updated", payload: dict | None = None, **changes) -> RunSnapshot:
        with self.transaction():
            run = self.get_run(run_id)
            if run.revision != expected_revision:
                raise RuntimeConflictError("stale runtime revision")
            target = status or run.status
            validate_transition(run.status, target)
            # Re-validation prevents model_copy/update bypass of persisted invariants.
            run = RunSnapshot.model_validate({**run.model_dump(), **changes,
                "status": target, "revision": run.revision + 1, "updated_at": now()})
            self._event(run, event, payload or {})
            self._write_run(run)
        return run

    def _write_run(self, run: RunSnapshot) -> None:
        self.db.execute("UPDATE runs SET revision=?,payload=? WHERE id=?", (
            run.revision, run.model_dump_json(), run.run_id))

    def _event(self, run: RunSnapshot, event: str, payload: dict) -> None:
        run.event_sequence += 1
        metric = {
            "recovery_started": "recovery_attempts", "recovery_blocked": "reconciliation_failures",
            "approval_invalidated": "approval_invalidations", "approval_expired": "approval_expirations",
            "batch_compensated": "compensated_batches", "effect_reconciled": "avoided_effect_replays",
            "acceptance_pending": "acceptance_waits",
        }.get(event)
        if metric:
            counts = dict(run.data.get("runtime_metrics", {}))
            counts[metric] = int(counts.get(metric, 0)) + 1
            run.data = {**run.data, "runtime_metrics": counts}
        safe = redact({"type": event, "payload": payload})
        self.db.execute("INSERT INTO events VALUES(?,?,?)", (
            run.run_id, run.event_sequence, json.dumps(safe)))

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        return [{"sequence": row[0], **json.loads(row[1])} for row in self.db.execute(
            "SELECT sequence,payload FROM events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (run_id, after, min(max(limit, 1), 500)),
        )]

    def put(self, kind: str, key: str, value: dict, run_id: str | None = None) -> None:
        self.db.execute("INSERT INTO values_store VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,run_id=excluded.run_id",
                        (kind, key, run_id, json.dumps(value, ensure_ascii=False)))

    def get(self, kind: str, key: str) -> dict | None:
        row = self.db.execute("SELECT payload FROM values_store WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def values(self, kind: str, run_id: str | None = None) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM values_store WHERE kind=?" + (" AND run_id=?" if run_id else "") + " ORDER BY rowid",
            (kind, run_id) if run_id else (kind,))]

    def write_blob(self, content: bytes) -> str:
        if len(content) > 32 * 1024 * 1024:
            raise RuntimePersistenceError("recovery blob exceeds size limit before execution")
        key = hashlib.sha256(content).hexdigest()
        path = self.blob_root / key
        if path.exists():
            if self.read_blob(key) != content:
                raise RuntimePersistenceError("recovery blob mismatch")
            return key
        fd, temp = tempfile.mkstemp(prefix=".blob-", dir=self.blob_root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            if os.name != "nt":
                directory_fd = os.open(self.blob_root, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return key

    def read_blob(self, key: str, *, max_bytes: int = 32 * 1024 * 1024) -> bytes:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise RuntimePersistenceError("invalid recovery blob key")
        path = self.blob_root / key
        if is_link(path):
            raise RuntimePersistenceError("linked recovery blob")
        if path.stat().st_size > max_bytes:
            raise RuntimePersistenceError("recovery blob exceeds size limit")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != key:
            raise RuntimePersistenceError("corrupt recovery blob")
        return content
