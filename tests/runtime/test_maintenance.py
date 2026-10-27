import json
import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from coding_agent.runtime.maintenance import RuntimeMaintenance, restore_backup
from coding_agent.runtime.models import RunSnapshot, RunStatus, RuntimeConflictError, RuntimePersistenceError
from coding_agent.runtime.store import RuntimeStore


@pytest.fixture
def store(tmp_path):
    with_store = RuntimeStore(tmp_path / "state")
    yield with_store
    with_store.close()


def add_run(store, status=RunStatus.SUCCEEDED):
    return store.create_run(RunSnapshot(session_id="s", workspace_id="workspace", status=status))


def test_backup_restore_roundtrip_preserves_events_and_blobs(store, tmp_path):
    run = add_run(store, RunStatus.INTERRUPTED)
    key = store.write_blob(b"private source")
    store.put("proposal", "p", {"blob": key}, run.run_id)
    before = store.events(run.run_id)
    backup = tmp_path / "backup"
    RuntimeMaintenance(store).backup(backup)
    restore_backup(backup, tmp_path / "restored")
    restored = RuntimeStore(tmp_path / "restored")
    try:
        assert restored.get_run(run.run_id) == store.get_run(run.run_id)
        assert restored.events(run.run_id) == before
        assert restored.read_blob(key) == b"private source"
    finally:
        restored.close()


@pytest.mark.parametrize("fault", ["blob", "database", "manifest", "schema", "traversal", "missing_blob"])
def test_restore_rejects_corrupt_or_incomplete_bundle(store, tmp_path, fault):
    run = add_run(store)
    key = store.write_blob(b"source")
    store.put("proposal", "p", {"blob": key}, run.run_id)
    backup = tmp_path / "backup"
    RuntimeMaintenance(store).backup(backup)
    manifest = json.loads((backup / "manifest.json").read_bytes())
    if fault == "blob":
        (backup / "recovery" / key).write_bytes(b"changed")
    elif fault == "database":
        (backup / "runtime.db").write_bytes(b"bad sqlite")
    elif fault == "manifest":
        (backup / "manifest.json").unlink()
    elif fault == "schema":
        manifest["schema_version"] = 999
    elif fault == "traversal":
        manifest["files"]["../outside"] = key
    else:
        del manifest["files"]["recovery/" + key]
    if fault in {"schema", "traversal", "missing_blob"}:
        (backup / "manifest.json").write_text(json.dumps(manifest))
    target = tmp_path / "restore"
    with pytest.raises((RuntimePersistenceError, OSError, sqlite3.DatabaseError)):
        restore_backup(backup, target)
    assert not target.exists()


def test_no_live_overwrite_and_second_writer(store, tmp_path):
    add_run(store)
    backup = tmp_path / "backup"
    maintenance = RuntimeMaintenance(store)
    maintenance.backup(backup)
    with pytest.raises(RuntimeConflictError):
        maintenance.backup(backup)
    with pytest.raises(RuntimeConflictError):
        restore_backup(backup, store.root)
    with pytest.raises(RuntimePersistenceError):
        RuntimeStore(store.root)


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.QUEUED, RunStatus.RECONCILING])
def test_no_maintenance_during_execution(store, tmp_path, status):
    add_run(store, status)
    with pytest.raises(RuntimeConflictError):
        RuntimeMaintenance(store).backup(tmp_path / "backup")


def test_retention_dry_run_backup_first_and_private_orphan_gc(store, tmp_path):
    run = add_run(store)
    key = store.write_blob(b"orphan")
    maintenance = RuntimeMaintenance(store)
    plan = maintenance.retention_plan(keep_days=0)
    assert plan["run_ids"] == [run.run_id]
    assert store.read_blob(key) == b"orphan"
    result = maintenance.prune(backup_directory=tmp_path / "backup", keep_days=0, max_blob_bytes=0)
    assert not store.runs() and result["deleted_blobs"] == 1 and result["quota_met"]
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    restored = RuntimeStore(tmp_path / "restored")
    try:
        assert restored.get_run(run.run_id)
        assert restored.read_blob(key) == b"orphan"
    finally:
        restored.close()


@pytest.mark.parametrize("kind", ["active", "operation", "process", "sandbox", "unversioned_process"])
def test_unresolved_material_never_pruned_even_under_zero_quota(store, tmp_path, kind):
    run = add_run(store, RunStatus.INTERRUPTED if kind == "active" else RunStatus.CANCELLED)
    key = store.write_blob(b"must retain")
    if kind == "operation":
        store.put("operation", "o", {"status": "uncertain", "before": {"a": key}}, run.run_id)
    elif kind in {"process", "sandbox"}:
        store.put(kind + "_lease", "lease", {"state": "reserved"}, run.run_id)
    elif kind == "unversioned_process":
        store.put("process_lease", "lease", {"state": "stopped"}, run.run_id)
    result = RuntimeMaintenance(store).prune(backup_directory=tmp_path / "backup", keep_days=0, max_blob_bytes=0)
    assert store.get_run(run.run_id)
    assert store.read_blob(key) == b"must retain"
    assert not result["quota_met"]


def test_backup_disk_failure_cannot_delete_state(store, tmp_path, monkeypatch):
    run = add_run(store)
    key = store.write_blob(b"retain")
    def fail(*_):
        raise OSError(28, "no space left")
    monkeypatch.setattr("coding_agent.runtime.maintenance._write", fail)
    with pytest.raises(OSError):
        RuntimeMaintenance(store).prune(backup_directory=tmp_path / "incomplete", keep_days=0)
    assert store.get_run(run.run_id)
    assert store.read_blob(key) == b"retain"
    assert not (tmp_path / "incomplete/manifest.json").exists()


def test_shared_blob_not_collected_while_referenced(store, tmp_path):
    run = add_run(store)
    key = store.write_blob(b"shared")
    store.put("legacy", "unrelated", {"blob": key})
    RuntimeMaintenance(store).prune(backup_directory=tmp_path / "backup", keep_days=0)
    assert not store.runs()
    assert store.read_blob(key) == b"shared"


@pytest.mark.parametrize("options", [{"keep_days": -1}, {"max_blob_bytes": -1}, {"keep_days": 0.5}])
def test_invalid_retention_options(store, options):
    with pytest.raises(ValueError):
        RuntimeMaintenance(store).retention_plan(**options)


def test_backup_files_are_private_on_posix(store, tmp_path):
    backup = tmp_path / "backup"
    RuntimeMaintenance(store).backup(backup)
    if os.name != "nt":
        assert backup.stat().st_mode & 0o077 == 0


def test_partial_restore_is_not_an_empty_executable_database(store, tmp_path, monkeypatch):
    add_run(store, RunStatus.INTERRUPTED)
    store.write_blob(b"restore material")
    backup = tmp_path / "backup"
    RuntimeMaintenance(store).backup(backup)
    def fail(*_):
        raise OSError(28, "disk full during restore")
    monkeypatch.setattr(RuntimeStore, "write_blob", fail)
    target = tmp_path / "partial"
    with pytest.raises(OSError):
        restore_backup(backup, target)
    assert (target / "RESTORE_IN_PROGRESS").exists()
    with pytest.raises(RuntimePersistenceError, match="incomplete restore"):
        RuntimeStore(target)


def test_uncommitted_crash_temp_is_backed_up_and_gc_is_conservative(store, tmp_path):
    run = add_run(store, RunStatus.INTERRUPTED)
    temporary = store.blob_root / ".blob-uncommitted123"
    temporary.write_bytes(b"partial source")
    maintenance = RuntimeMaintenance(store)
    maintenance.prune(backup_directory=tmp_path / "backup", keep_days=0)
    assert temporary.read_bytes() == b"partial source"
    restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored/recovery/.blob-uncommitted123").read_bytes() == b"partial source"
    store.update(run.run_id, store.get_run(run.run_id).revision, status=RunStatus.CANCELLED)
    maintenance.prune(backup_directory=tmp_path / "backup2", keep_days=0)
    assert not temporary.exists()


def test_backup_rejects_symlink_components_without_reading_outside(store, tmp_path, monkeypatch):
    backup = tmp_path / "linked"
    monkeypatch.setattr("coding_agent.runtime.maintenance.is_link", lambda path: path == backup)
    with pytest.raises(RuntimePersistenceError, match="linked"):
        RuntimeMaintenance(store).backup(backup)
    assert not backup.exists()


def test_sqlite_readonly_prune_preserves_authoritative_state(store, tmp_path):
    run = add_run(store)
    key = store.write_blob(b"retain on readonly failure")
    store.db.close()
    store.db = sqlite3.connect((store.root / "runtime.db").as_uri() + "?mode=ro", uri=True, isolation_level=None)
    store.db.row_factory = sqlite3.Row
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        RuntimeMaintenance(store).prune(backup_directory=tmp_path / "backup", keep_days=0)
    assert store.get_run(run.run_id).revision == run.revision
    assert store.read_blob(key) == b"retain on readonly failure"
    assert not store.db.in_transaction


def test_sqlite_full_transaction_cannot_publish_partial_revision(store):
    run = add_run(store)
    before = store.events(run.run_id)
    pages = store.db.execute("PRAGMA page_count").fetchone()[0]
    store.db.execute(f"PRAGMA max_page_count={pages}")
    with pytest.raises(sqlite3.DatabaseError, match="full"):
        store.update(run.run_id, run.revision, payload={"large": "x" * 1024 * 1024})
    assert store.get_run(run.run_id).revision == run.revision
    assert store.events(run.run_id) == before
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_compact_reclaims_pages_only_after_a_private_backup(store, tmp_path):
    run = add_run(store)
    store.put("large_fixture", "x", {"payload": "x" * 1024 * 1024}, run.run_id)
    maintenance = RuntimeMaintenance(store)
    maintenance.prune(backup_directory=tmp_path / "before-prune", keep_days=0)
    before = maintenance.inspect()["database_bytes"]
    after = maintenance.compact(backup_directory=tmp_path / "before-compact")
    assert after["integrity"] == "ok" and after["database_bytes"] < before
    assert (tmp_path / "before-compact/manifest.json").is_file()


def test_v11_database_upgrades_without_rewriting_runs_or_events(tmp_path):
    root = tmp_path / "v11"
    original = RuntimeStore(root)
    run = add_run(original, RunStatus.INTERRUPTED)
    events = original.events(run.run_id)
    original.close()
    with sqlite3.connect(root / "runtime.db") as database:
        database.execute("PRAGMA user_version=1")
    database.close()
    upgraded = RuntimeStore(root)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert upgraded.get_run(run.run_id) == run
        assert upgraded.events(run.run_id) == events
    finally:
        upgraded.close()


def test_v11_backup_restores_to_current_protocol_not_an_old_writer(store, tmp_path):
    run = add_run(store, RunStatus.INTERRUPTED)
    backup = tmp_path / "v11-backup"
    RuntimeMaintenance(store).backup(backup)
    database = backup / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=1")
    connection.close()
    manifest = json.loads((backup / "manifest.json").read_bytes())
    manifest["schema_version"] = 1
    manifest["files"]["runtime.db"] = hashlib.sha256(database.read_bytes()).hexdigest()
    (backup / "manifest.json").write_text(json.dumps(manifest))
    restore_backup(backup, tmp_path / "restored-v11")
    restored = RuntimeStore(tmp_path / "restored-v11")
    try:
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert restored.get_run(run.run_id) == run
    finally:
        restored.close()
