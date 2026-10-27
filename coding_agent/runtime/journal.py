"""Write-ahead file intent and conservative crash reconciliation.

Only explicit file operations are replayable. Matching post-state proves the
desired effect exists, not who performed it or exactly how often it executed.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from coding_agent.filesystem.atomic_write import atomic_write_bytes_checked
from coding_agent.filesystem.checked_delete import delete_file_checked
from coding_agent.filesystem.checked_move import move_file_checked
from coding_agent.filesystem.encoding import encode_text_file
from coding_agent.filesystem.versions import version_of_file, version_from_bytes
from coding_agent.mutation.models import PlannedCreate, PlannedEdit, PlannedDelete, PlannedMove
from coding_agent.runtime.models import OperationStatus as S, RecoveryRequiredError, OperationRecord
from coding_agent.runtime.privacy import is_link


def safe_file(workspace, relative: str) -> Path:
    """Re-check every component. Recovery never follows even in-workspace links."""
    resolved = workspace.resolve(relative)
    current = workspace.root
    for component in Path(relative).parts:
        current = current / component
        if is_link(current):
            raise RecoveryRequiredError("linked recovery target")
    if resolved.exists() and not resolved.is_file():
        raise RecoveryRequiredError("recovery target is not a regular file")
    return resolved


class MutationJournal:
    def __init__(self, store, workspace, run_id: str, proposal_id: str):
        self.store, self.workspace = store, workspace
        self.run_id, self.proposal_id = run_id, proposal_id

    def rows(self):
        values = [OperationRecord.model_validate(row).model_dump(mode="json")
                  for row in self.store.values("operation", self.run_id)]
        if any(row["run_id"] != self.run_id for row in values):
            raise RecoveryRequiredError("operation belongs to another run")
        return sorted((row for row in values
                       if row["proposal_id"] == self.proposal_id), key=lambda row: row["index"])

    def prepare(self, planned) -> None:
        previous = self.rows()
        if previous and (len(previous) != len(planned.operations)
                         or any(row["status"] != S.PREPARED.value for row in previous)):
            raise RecoveryRequiredError("an existing journal must be reconciled, never overwritten")
        rows = []
        for index, operation in enumerate(planned.operations):
            if isinstance(operation, PlannedMove):
                paths = [operation.source, operation.destination]
            else:
                paths = [operation.path]
            before = {}
            for path in paths:
                target = safe_file(self.workspace, path)
                before[path] = self.store.write_blob(target.read_bytes()) if target.exists() else None
            if isinstance(operation, PlannedCreate):
                after = {operation.path: self.store.write_blob(encode_text_file(operation.content, newline="LF", has_bom=False))}
            elif isinstance(operation, PlannedEdit):
                if before[operation.path] != operation.snapshot.version.digest:
                    raise RecoveryRequiredError("file changed while preparing recovery data")
                after = {operation.path: self.store.write_blob(encode_text_file(operation.new_content,
                    newline=operation.snapshot.newline, has_bom=operation.snapshot.has_bom))}
            elif isinstance(operation, PlannedDelete):
                if before[operation.path] != operation.snapshot.version.digest:
                    raise RecoveryRequiredError("delete precondition changed")
                after = {operation.path: None}
            elif isinstance(operation, PlannedMove):
                if before[operation.source] != operation.source_snapshot.version.digest or before[operation.destination] is not None:
                    raise RecoveryRequiredError("move precondition changed")
                after = {operation.source: None, operation.destination: before[operation.source]}
            else:
                raise RecoveryRequiredError("unrecognized file operation")
            rows.append({"operation_id": str(uuid4()), "run_id": self.run_id,
                "proposal_id": self.proposal_id, "index": index, "kind": operation.kind,
                "status": S.PREPARED.value, "before": before, "after": after})
        if previous:
            if any((old["kind"], old["before"], old["after"]) != (new["kind"], new["before"], new["after"])
                   for old, new in zip(previous, rows)):
                raise RecoveryRequiredError("actual operation differs from approved proposal")
            return
        with self.store.transaction():
            for row in rows:
                self.store.put("operation", row["operation_id"], row, self.run_id)

    def start(self, index: int) -> None:
        row = self.rows()[index]
        if row["status"] != S.PREPARED.value or not self.matches(row["before"]):
            raise RecoveryRequiredError("file intent is no longer executable")
        self._set(row, S.APPLYING)

    def applied(self, index: int) -> None:
        row = self.rows()[index]
        if not self.matches(row["after"]):
            raise RecoveryRequiredError("file effect differs from approved intent")
        self._set(row, S.APPLIED)

    def rolled_back(self) -> None:
        for row in self.rows():
            if not self.matches(row["before"]):
                self._set(row, S.UNCERTAIN)
                raise RecoveryRequiredError("rollback did not restore recorded preconditions")
            self._set(row, S.ROLLED_BACK)

    def matches(self, states: dict[str, str | None]) -> bool:
        for relative, expected in states.items():
            path = safe_file(self.workspace, relative)
            if expected is None:
                if path.exists():
                    return False
            elif not path.is_file() or version_of_file(path).digest != expected:
                return False
        return True

    def reconcile(self) -> str:
        rows = self.rows()
        if not rows:
            return "not_started"
        if all(row["status"] == S.ABANDONED.value for row in rows):
            return "abandoned"
        # Validate every blob BEFORE attempting any restoration.
        for row in rows:
            for key in set(row["before"].values()) | set(row["after"].values()):
                if key is not None:
                    self.store.read_blob(key)
        states = []
        for row in rows:
            before, after = self.matches(row["before"]), self.matches(row["after"])
            if row["status"] == S.APPLIED.value and not after:
                raise RecoveryRequiredError("a completed operation was changed externally")
            if row["status"] == S.ROLLED_BACK.value and not before:
                raise RecoveryRequiredError("a rolled-back operation was changed externally")
            if row["status"] == S.UNCERTAIN.value or not (before or after):
                raise RecoveryRequiredError("unknown file effect; preserving user data")
            states.append("after" if after and row["status"] not in {S.PREPARED.value, S.ROLLED_BACK.value} else "before")
            if row["status"] == S.PREPARED.value and not before:
                raise RecoveryRequiredError("unstarted operation changed externally")
        if all(state == "after" for state in states):
            for row in rows:
                self._set(row, S.APPLIED)
            return "applied"
        if all(state == "before" for state in states) and all(
                row["status"] in {S.PREPARED.value, S.APPLYING.value} for row in rows):
            for row in rows:
                self._set(row, S.PREPARED)
            return "not_started"
        # Partial batches are compensated, not continued from an unapproved mix.
        for row, state in reversed(list(zip(rows, states))):
            if state == "after":
                self._set(row, S.ROLLING_BACK)
                self._restore(row)
            self._set(row, S.ROLLED_BACK)
        return "rolled_back"

    def _restore(self, row) -> None:
        if not self.matches(row["after"]):
            raise RecoveryRequiredError("external edit during recovery")
        if row["kind"] == "move_file":
            source, destination = list(row["before"])
            current = safe_file(self.workspace, destination)
            expected = version_from_bytes(self.store.read_blob(row["after"][destination]))
            move_file_checked(current, safe_file(self.workspace, source), expected_source_version=expected)
        else:
            relative, old_key = next(iter(row["before"].items()))
            path = safe_file(self.workspace, relative)
            after_key = row["after"][relative]
            expected = version_from_bytes(self.store.read_blob(after_key)) if after_key else None
            if old_key is None:
                delete_file_checked(path, expected_version=expected)
            elif path.exists():
                atomic_write_bytes_checked(path, self.store.read_blob(old_key), expected_version=expected)
            else:
                # Exclusive creation: do not replace a concurrently-created user file.
                with path.open("xb") as handle:
                    import os
                    handle.write(self.store.read_blob(old_key))
                    handle.flush()
                    os.fsync(handle.fileno())
        if not self.matches(row["before"]):
            raise RecoveryRequiredError("incomplete recovery")

    def _set(self, row, status: S) -> None:
        row = {**row, "status": status.value}
        self.store.put("operation", row["operation_id"], row, self.run_id)
