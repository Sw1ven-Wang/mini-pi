import hashlib
from pathlib import Path

import pytest

from coding_agent.changes.workspace_ir import (
    EditFileOp,
    TextReplacement,
    WorkspaceChangeSet,
)
from coding_agent.filesystem.versions import FileVersion, version_of_file
from coding_agent.replay.recorder import TraceRecorder
from coding_agent.replay.replay import TraceReplayer
from coding_agent.verification.models import VerificationResult, VerificationSummary, VerificationKind


def _record(change_set):
    recorder = TraceRecorder()
    recorder.record_action(change_set)
    return recorder.finalize(trace_id="trace", session_id="session")


async def _verify(workspace):
    passed = workspace.resolve("value.txt").read_text() == "after\n"
    return VerificationSummary(
        passed=passed,
        results=[VerificationResult(
            kind=VerificationKind.CUSTOM, name="content", passed=passed
        )],
    )


async def test_replay_uses_temp_never_changes_source_and_reproduces_state(tmp_path: Path):
    target = tmp_path / "value.txt"
    target.write_text("before\n")
    original = target.read_bytes()
    record = _record(WorkspaceChangeSet(operations=[EditFileOp(
        path="value.txt", expected_version=version_of_file(target),
        replacements=[TextReplacement(old_text="before", new_text="after")],
    )]))
    result = await TraceReplayer().replay(
        record, source_workspace=tmp_path, verifier=_verify
    )
    assert result.used_temporary_workspace is True
    assert result.source_unchanged is True
    assert target.read_bytes() == original
    expected = original.replace(b"before", b"after")
    assert result.final_files["value.txt"] == hashlib.sha256(expected).hexdigest()
    assert result.verification_summary["passed"] is True


async def test_replay_stops_on_version_divergence(tmp_path: Path):
    target = tmp_path / "value.txt"
    target.write_text("before\n")
    bad = FileVersion(digest="0" * 64, size_bytes=7)
    record = _record(WorkspaceChangeSet(operations=[EditFileOp(
        path="value.txt", expected_version=bad,
        replacements=[TextReplacement(old_text="before", new_text="after")],
    )]))
    with pytest.raises(Exception) as caught:
        await TraceReplayer().replay(record, source_workspace=tmp_path)
    assert getattr(caught.value, "code", None).value == "stale_version"
    assert target.read_text() == "before\n"
