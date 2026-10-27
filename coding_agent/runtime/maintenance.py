"""Explicit private backups and conservative retention under the writer lock.

Backups contain source bytes and are never audit exports. Restore is offline,
into a new destination only; incomplete bundles have no committed manifest.
"""

from datetime import timedelta
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import re

from coding_agent.runtime.models import TERMINAL, RunStatus, RuntimeConflictError, RuntimePersistenceError, now
from coding_agent.runtime.privacy import is_link, private_directory
from coding_agent.runtime.store import RuntimeStore, SCHEMA_VERSION


def _write(path, content):
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _safe_path(path):
    path = Path(path).absolute()
    if any(is_link(part) for part in (path, *path.parents)):
        raise RuntimePersistenceError("linked maintenance path")
    return path


def _hash(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _references(value):
    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return {value}
    if isinstance(value, dict):
        return set().union(*(_references(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_references(item) for item in value), set())
    return set()


def _uncommitted(name):
    return re.fullmatch(r"\.blob-[a-zA-Z0-9_-]{1,128}", name) is not None


class RuntimeMaintenance:
    def __init__(self, store: RuntimeStore):
        self.store = store

    def _idle(self):
        if self.store.db.in_transaction or any(run.status in {
                RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.RECONCILING} for run in self.store.runs()):
            raise RuntimeConflictError("maintenance requires stopped executors")

    def inspect(self):
        result = self.store.db.execute("PRAGMA quick_check").fetchall()
        if [row[0] for row in result] != ["ok"]:
            raise RuntimePersistenceError("runtime integrity check failed")
        blobs = list(self.store.blob_root.iterdir())
        if any(is_link(path) or not path.is_file() for path in blobs):
            raise RuntimePersistenceError("invalid recovery entry")
        return {"schema_version": SCHEMA_VERSION, "integrity": "ok",
                "runs": len(self.store.runs()), "blob_count": len(blobs),
                "blob_bytes": sum(path.stat().st_size for path in blobs),
                "database_bytes": sum(path.stat().st_size for path in self.store.root.glob("runtime.db*") if path.is_file())}

    def backup(self, destination: Path) -> dict:
        self._idle()
        destination = _safe_path(destination)
        if destination.exists() or destination.is_relative_to(self.store.root):
            raise RuntimeConflictError("backup requires a new directory outside runtime storage")
        self.inspect()
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        private_directory(destination)
        blobs = private_directory(destination / "recovery")
        database = destination / "runtime.db"
        target = sqlite3.connect(database)
        try:
            self.store.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimePersistenceError("backup database is corrupt")
        finally:
            target.close()
        with database.open("r+b") as handle:
            os.fsync(handle.fileno())
        files = {"runtime.db": _hash(database)}
        for path in self.store.blob_root.iterdir():
            # A hard crash during write_blob can leave an uncommitted temp file.
            # Preserve it in private backups; it is never an executable proposal.
            if _uncommitted(path.name):
                if path.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimePersistenceError("oversized uncommitted blob")
                content = path.read_bytes()
            else:
                content = self.store.read_blob(path.name)
            _write(blobs / path.name, content)
            files["recovery/" + path.name] = hashlib.sha256(content).hexdigest()
        manifest = {"format_version": 1, "schema_version": SCHEMA_VERSION,
                    "created_at": now().isoformat(), "files": files}
        _write(destination / "manifest.json", json.dumps(manifest, sort_keys=True).encode())
        if os.name != "nt":
            for directory in (blobs, destination, destination.parent):
                fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        return {"file_count": len(files), "schema_version": SCHEMA_VERSION}

    def retention_plan(self, *, keep_days=30, max_blob_bytes=None) -> dict:
        self._idle()
        if not isinstance(keep_days, int) or keep_days < 0 or (max_blob_bytes is not None and
                (not isinstance(max_blob_bytes, int) or max_blob_bytes < 0)):
            raise ValueError("invalid retention limits")
        cutoff = now() - timedelta(days=keep_days)
        eligible, protected = [], []
        for run in self.store.runs():
            operations = self.store.values("operation", run.run_id)
            resources = [*self.store.values("process_lease", run.run_id), *self.store.values("sandbox_lease", run.run_id)]
            uncertain = any(row.get("status") not in {"applied", "rolled_back", "abandoned"} for row in operations)
            uncertain |= any(row.get("state") != "stopped" for row in resources)
            uncertain |= any(row.get("ownership_protocol") != 1 for row in self.store.values("process_lease", run.run_id))
            if run.status in TERMINAL and run.updated_at <= cutoff and not uncertain:
                eligible.append(run.run_id)
            else:
                protected.append(run.run_id)
        return {"run_ids": eligible, "protected_run_ids": protected,
                "keep_days": keep_days, "max_blob_bytes": max_blob_bytes,
                "storage": self.inspect()}

    def compact(self, *, backup_directory: Path) -> dict:
        self.backup(backup_directory)
        self.store.db.execute("VACUUM")
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return self.inspect()

    def prune(self, *, backup_directory: Path, keep_days=30, max_blob_bytes=None) -> dict:
        plan = self.retention_plan(keep_days=keep_days, max_blob_bytes=max_blob_bytes)
        # Backup commits first. Any failure leaves all authoritative data intact.
        self.backup(backup_directory)
        with self.store.transaction():
            for run_id in plan["run_ids"]:
                self.store.db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
                self.store.db.execute("DELETE FROM values_store WHERE run_id=?", (run_id,))
                self.store.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            sessions = {run.session_id for run in self.store.runs()}
            for row in self.store.db.execute("SELECT id FROM values_store WHERE kind='session'").fetchall():
                if row[0] not in sessions:
                    # Keep legacy imports; only remove sessions explicitly linked
                    # to a pruned Run, never unrelated SDK/legacy sessions.
                    value = self.store.get("session", row[0])
                    if value.get("state", {}).get("active_run_id") in plan["run_ids"]:
                        self.store.db.execute("DELETE FROM values_store WHERE kind='session' AND id=?", (row[0],))
        deleted = 0
        # Conservatively retain ALL blobs while any unresolved journal/run remains.
        safe_gc = all(run.status in TERMINAL and all(row.get("status") in {
            "applied", "rolled_back", "abandoned"} for row in self.store.values("operation", run.run_id))
            and all(row.get("state") == "stopped" for row in [*self.store.values("process_lease", run.run_id),
                *self.store.values("sandbox_lease", run.run_id)])
            and all(row.get("ownership_protocol") == 1 for row in self.store.values("process_lease", run.run_id))
            for run in self.store.runs())
        if safe_gc:
            refs = set()
            for row in self.store.db.execute("SELECT payload FROM values_store UNION ALL SELECT payload FROM runs"):
                refs |= _references(json.loads(row[0]))
            for path in self.store.blob_root.iterdir():
                if path.name not in refs:
                    # Validate exact hash-shaped file, reject links before unlink.
                    if not _uncommitted(path.name):
                        self.store.read_blob(path.name)
                    elif is_link(path):
                        raise RuntimePersistenceError("linked uncommitted blob")
                    path.unlink()
                    deleted += 1
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        storage = self.inspect()
        return {**plan, "deleted_blobs": deleted, "storage": storage,
                "quota_met": max_blob_bytes is None or storage["blob_bytes"] <= max_blob_bytes}


def restore_backup(source: Path, destination: Path) -> None:
    """No in-place rollback of live state, no implicit schema downgrade."""
    source, destination = _safe_path(source), _safe_path(destination)
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise RuntimeConflictError("restore requires a new independent state directory")
    manifest_path = _safe_path(source / "manifest.json")
    if manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise RuntimePersistenceError("oversized backup manifest")
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("format_version") != 1 or manifest.get("schema_version") not in (1, SCHEMA_VERSION):
        raise RuntimePersistenceError("unsupported backup format/schema")
    files = manifest.get("files", {})
    if "runtime.db" not in files:
        raise RuntimePersistenceError("missing backup database")
    for relative, checksum in files.items():
        blob = relative.startswith("recovery/") and len(relative) == 73 and all(c in "0123456789abcdef" for c in relative[9:]) and checksum == relative[9:]
        temporary = relative.startswith("recovery/") and _uncommitted(relative[9:])
        if relative != "runtime.db" and not (blob or temporary):
            raise RuntimePersistenceError("invalid backup file identity")
        path = _safe_path(source / relative)
        if relative != "runtime.db" and path.stat().st_size > 32 * 1024 * 1024:
            raise RuntimePersistenceError("oversized backup blob")
        if _hash(path) != checksum:
            raise RuntimePersistenceError("backup checksum mismatch")
    database = source / "runtime.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
        if (original.execute("PRAGMA user_version").fetchone()[0] != manifest["schema_version"]
                or original.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
            raise RuntimePersistenceError("invalid backup database")
        # Only reference values that are blob fields, not arbitrary check digests,
        # must exist. Operations and proposals define the complete blob contract.
        for row in original.execute("SELECT kind,payload FROM values_store WHERE kind IN ('operation','proposal')"):
            value = json.loads(row[1])
            required = (_references([value["before"], value["after"]]) if row[0] == "operation" else {value["blob"]})
            if any("recovery/" + key not in files for key in required):
                raise RuntimePersistenceError("backup missing referenced recovery data")
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        private_directory(destination)
        marker = destination / "RESTORE_IN_PROGRESS"
        _write(marker, b"incomplete private restore; do not execute")
        if os.name != "nt":
            fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        staging = destination / ".restore"
        with_store = RuntimeStore(staging)
        try:
            for relative in files:
                if relative.startswith("recovery/"):
                    content = (source / relative).read_bytes()
                    if _uncommitted(relative[9:]):
                        _write(with_store.blob_root / relative[9:], content)
                    else:
                        with_store.write_blob(content)
            original.backup(with_store.db)
            with with_store.transaction():
                with_store.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            with_store.close()
        # RuntimeStore refuses the destination until both files and blobs have
        # been published. A failed/crashed restore can never appear as empty state.
        os.replace(staging / "runtime.db", destination / "runtime.db")
        os.replace(staging / "recovery", destination / "recovery")
        if os.name != "nt":
            fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        marker.unlink()
        if os.name != "nt":
            fd = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
