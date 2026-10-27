"""Actual offline maintenance subprocesses, not mocked CLI handlers."""

import json
import subprocess
import sys

from coding_agent.filesystem.workspace import Workspace
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.runtime.models import RunStatus


def cli(workspace, state, *args):
    return subprocess.run([sys.executable, "-m", "coding_agent.runtime", "--workspace", str(workspace),
        "--state-root", str(state), *map(str, args)], capture_output=True, text=True, timeout=30)


def test_inspect_typo_does_not_create_an_empty_database(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "missing"
    result = cli(workspace, state, "inspect")
    assert result.returncode == 2
    assert "no existing runtime database" in result.stderr
    assert not state.exists()


def test_backup_restore_and_dry_run_cli_roundtrip(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("preserve")
    state = tmp_path / "private"
    kernel = RuntimeKernel(Workspace(workspace), state_root=state)
    execution = kernel.start("session", command="test", goal="goal")
    execution.kernel.cancel(execution.run_id)
    kernel.close()
    backup = tmp_path / "backup"
    result = cli(workspace, state, "backup", "--destination", backup)
    assert result.returncode == 0, result.stderr
    restored_root = tmp_path / "restored"
    result = cli(workspace, restored_root, "restore", "--source", backup)
    assert result.returncode == 0, result.stderr
    restored = RuntimeKernel(Workspace(workspace), state_root=restored_root)
    try:
        assert restored.snapshot(execution.run_id).status == RunStatus.CANCELLED
    finally:
        restored.close()
    result = cli(workspace, state, "prune", "--keep-days", "0")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["run_ids"] == [execution.run_id]
    result = cli(workspace, state, "compact", "--backup-directory", tmp_path / "before-compact")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["integrity"] == "ok"
    original = RuntimeKernel(Workspace(workspace), state_root=state)
    try:
        assert original.snapshot(execution.run_id).status == RunStatus.CANCELLED
    finally:
        original.close()
    assert (workspace / "source.txt").read_text() == "preserve"


def test_unmet_quota_cli_is_nonzero_and_retains_unresolved_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "private"
    kernel = RuntimeKernel(Workspace(workspace), state_root=state)
    execution = kernel.start("session", command="test")
    key = execution.store.write_blob(b"unresolved material")
    kernel.mark_interrupted()
    kernel.close()
    result = cli(workspace, state, "prune", "--keep-days", "0", "--max-blob-bytes", "0",
                 "--apply", "--backup-directory", tmp_path / "backup")
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["quota_met"] is False
    restored = RuntimeKernel(Workspace(workspace), state_root=state)
    try:
        assert restored.snapshot(execution.run_id).status == RunStatus.INTERRUPTED
        assert restored.store.read_blob(key) == b"unresolved material"
    finally:
        restored.close()
