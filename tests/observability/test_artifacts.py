import hashlib
from pathlib import Path

import pytest

from coding_agent.observability.artifacts import ArtifactRef, ArtifactStore


def test_artifact_roundtrip(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("pytest output", kind="test-log")
    assert store.read(reference) == b"pytest output"


def test_artifact_hash(tmp_path: Path):
    reference = ArtifactStore(tmp_path).put_bytes(b"abc", kind="diff")
    assert reference.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert reference.artifact_id == reference.sha256


def test_artifact_dedup(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    first = store.put_text("same", kind="log")
    second = store.put_text("same", kind="diff")
    assert first.relative_path == second.relative_path
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1


def test_artifact_no_escape(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.read(
            ArtifactRef.model_construct(
                artifact_id="../escape",
                kind="log",
                sha256="../escape",
                size_bytes=0,
                relative_path="../escape",
            )
        )


def test_artifact_atomic_write(tmp_path: Path, monkeypatch):
    store = ArtifactStore(tmp_path)

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("coding_agent.observability.artifacts.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.put_text("content", kind="log")
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


def test_text_artifact_redacts_inline_secret(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    reference = store.put_text("Authorization: Bearer abcdef", kind="log")
    assert b"abcdef" not in store.read(reference)
