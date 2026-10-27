"""Tests for checked delete + delete rollback."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.rollback import RollbackManager
from coding_agent.errors import (
    FileNotFoundAgentError,
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.checked_delete import (
    ExpectedFileState,
    delete_file_checked,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace


def _run(coro):
    return asyncio.run(coro)


def _rollback_manager(
    tmp_path: Path,
    store: CheckpointStore | None = None,
) -> tuple[RollbackManager, CheckpointStore]:
    store = store or CheckpointStore()
    return (
        RollbackManager(
            Workspace(tmp_path),
            FileMutationQueue(),
            store,
        ),
        store,
    )


# ---------------------------------------------------------------------------
# checked delete primitive
# ---------------------------------------------------------------------------


def test_delete_file_success(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")

    version = version_of_file(tmp_path / "a.py")

    old_version = delete_file_checked(
        tmp_path / "a.py",
        expected_version=version,
    )

    assert old_version.digest == version.digest
    assert not (tmp_path / "a.py").exists()


def test_delete_returns_old_version(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")

    version = version_of_file(tmp_path / "a.py")

    old = delete_file_checked(
        tmp_path / "a.py",
        expected_version=version,
    )

    assert old.digest == version.digest
    assert old.size_bytes == version.size_bytes


def test_delete_stale_rejected(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")

    stale = version_of_file(tmp_path / "a.py")

    (tmp_path / "a.py").write_text("x = 2\n")

    with pytest.raises(StaleVersionError):
        delete_file_checked(
            tmp_path / "a.py",
            expected_version=stale,
        )

    assert (tmp_path / "a.py").exists()


def test_delete_missing_rejected(tmp_path: Path):
    with pytest.raises(FileNotFoundAgentError):
        delete_file_checked(
            tmp_path / "missing.py",
            expected_version=version_of_file(
                tmp_path / "missing.py"
            ) if False else _dummy_version(),
        )


def _dummy_version():
    from coding_agent.filesystem.versions import FileVersion

    return FileVersion(digest="x", size_bytes=1)


def test_delete_directory_rejected(tmp_path: Path):
    (tmp_path / "subdir").mkdir()

    from coding_agent.filesystem.versions import FileVersion

    with pytest.raises(InvalidArgumentError):
        delete_file_checked(
            tmp_path / "subdir",
            expected_version=FileVersion(
                digest="x", size_bytes=1
            ),
        )


# ---------------------------------------------------------------------------
# delete rollback
# ---------------------------------------------------------------------------


def test_delete_rollback_restores_exact_bytes(tmp_path: Path):
    file = tmp_path / "a.py"
    original = b"x = 1\n"
    file.write_bytes(original)

    store = CheckpointStore()
    manager = RollbackManager(
        Workspace(tmp_path),
        FileMutationQueue(),
        store,
    )

    version = version_of_file(file)

    checkpoint = store.create(
        path=file,
        workspace_relative="a.py",
    )

    # 删除。
    delete_file_checked(file, expected_version=version)

    assert not file.exists()

    # 回滚。
    result = _run(
        manager.rollback_state(
            checkpoint.id,
            expected_current_state=ExpectedFileState(
                exists=False,
            ),
        )
    )

    assert file.read_bytes() == original
    assert result.new_version.digest == version.digest


def test_delete_rollback_preserves_bom(tmp_path: Path):
    file = tmp_path / "a.py"
    original = b"\xef\xbb\xbf" b"x = 1\n"
    file.write_bytes(original)

    manager, store = _rollback_manager(tmp_path)

    version = version_of_file(file)

    checkpoint = store.create(
        path=file,
        workspace_relative="a.py",
    )

    delete_file_checked(file, expected_version=version)

    _run(
        manager.rollback_state(
            checkpoint.id,
            expected_current_state=ExpectedFileState(
                exists=False,
            ),
        )
    )

    assert file.read_bytes() == original


def test_delete_rollback_preserves_crlf(tmp_path: Path):
    file = tmp_path / "a.py"
    original = b"x = 1\r\ny = 2\r\n"
    file.write_bytes(original)

    manager, store = _rollback_manager(tmp_path)

    version = version_of_file(file)

    checkpoint = store.create(
        path=file,
        workspace_relative="a.py",
    )

    delete_file_checked(file, expected_version=version)

    _run(
        manager.rollback_state(
            checkpoint.id,
            expected_current_state=ExpectedFileState(
                exists=False,
            ),
        )
    )

    assert file.read_bytes() == original


def test_delete_rollback_rejects_recreated_file(tmp_path: Path):
    file = tmp_path / "a.py"
    file.write_bytes(b"original\n")

    manager, store = _rollback_manager(tmp_path)

    version = version_of_file(file)

    checkpoint = store.create(
        path=file,
        workspace_relative="a.py",
    )

    delete_file_checked(file, expected_version=version)

    # 用户重新创建了同名文件。
    file.write_bytes(b"user recreated\n")

    with pytest.raises(StaleVersionError):
        _run(
            manager.rollback_state(
                checkpoint.id,
                expected_current_state=ExpectedFileState(
                    exists=False,
                ),
            )
        )

    # 用户文件未被覆盖。
    assert file.read_bytes() == b"user recreated\n"


def test_delete_creates_checkpoint(tmp_path: Path):
    file = tmp_path / "a.py"
    file.write_bytes(b"content\n")

    manager, store = _rollback_manager(tmp_path)

    version = version_of_file(file)

    checkpoint = store.create(
        path=file,
        workspace_relative="a.py",
    )

    assert checkpoint.existed is True
    assert checkpoint.content == b"content\n"
    assert checkpoint.version.digest == version.digest


def test_expected_file_state_validation():
    # exists=True 但 version=None → 报错
    with pytest.raises(ValueError):
        ExpectedFileState(exists=True, version=None)

    # exists=False 但 version 非 None → 报错
    with pytest.raises(ValueError):
        ExpectedFileState(
            exists=False,
            version=_dummy_version(),
        )
