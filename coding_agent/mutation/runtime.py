"""WorkspaceMutationRuntime：plan → apply → recover。

不包含 LLM Tool schema / Permission / Prompt。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.rollback import RollbackManager
from coding_agent.changes.workspace_ir import WorkspaceChangeSet
from coding_agent.editing.patch import PatchRuntime
from coding_agent.errors import (
    WorkspaceRecoveryError,
)
from coding_agent.filesystem.atomic_write import (
    atomic_write_bytes,
)
from coding_agent.filesystem.checked_delete import (
    ExpectedFileState,
    delete_file_checked,
)
from coding_agent.filesystem.checked_move import (
    move_file_checked,
)
from coding_agent.filesystem.encoding import encode_text_file
from coding_agent.filesystem.mutation_queue import (
    FileMutationQueue,
)
from coding_agent.filesystem.versions import (
    same_version,
    version_of_file,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.mutation.models import (
    MoveRecoveryRecord,
    PlannedCreate,
    PlannedDelete,
    PlannedEdit,
    PlannedMove,
    PlannedWorkspaceChangeSet,
    RecoveryFailure,
    RecoveryReport,
    WorkspaceMutationResult,
)
from coding_agent.mutation.planner import (
    plan_workspace_change_set,
)
from coding_agent.changes.models import MutationResult
from coding_agent.testing.fault_injection import NoopFaultInjector


class WorkspaceMutationRuntime:
    """对 WorkspaceChangeSet 做 plan-then-apply，含 reverse recovery。"""

    def __init__(
        self,
        workspace: Workspace,
        mutation_queue: FileMutationQueue,
        checkpoint_store: CheckpointStore,
        fault_injector=None,
        lifecycle_observer=None,
        journal=None,
    ) -> None:
        self.workspace = workspace
        self.mutation_queue = mutation_queue
        self.checkpoint_store = checkpoint_store
        self.fault_injector = fault_injector or NoopFaultInjector()
        self.lifecycle_observer = lifecycle_observer
        self.journal = journal
        self.rollback_manager = RollbackManager(
            workspace,
            mutation_queue,
            checkpoint_store,
        )

    async def apply(
        self,
        change_set: WorkspaceChangeSet,
    ) -> WorkspaceMutationResult:
        # --------------------------------------------------
        # 1. plan（只读）
        # --------------------------------------------------

        planned = plan_workspace_change_set(
            self.workspace,
            change_set,
        )
        if self.journal is not None:
            # Critical persistence, deliberately not a best-effort observer.
            self.journal.prepare(planned)
            self.fault_injector.maybe_fail("after_journal_prepare")

        applied: list[_AppliedRecord] = []

        try:
            async with AsyncExitStack() as stack:
                # ------------------------------------------
                # 2. 按 canonical resolved path 稳定排序加锁
                # ------------------------------------------

                for lock_path in planned.lock_paths:
                    await stack.enter_async_context(
                        self.mutation_queue.hold(
                            Path(lock_path)
                        )
                    )

                # ------------------------------------------
                # 3. final recheck + apply
                # ------------------------------------------

                for index, operation in enumerate(
                    planned.operations
                ):
                    if self.journal is not None:
                        self.journal.start(index)
                        self.fault_injector.maybe_fail("before_operation_effect")
                    if index == 1:
                        self.fault_injector.maybe_fail("before_second_file_write")
                    record = await self._apply_one(
                        index,
                        operation,
                    )
                    self.fault_injector.maybe_fail("after_operation_effect")
                    applied.append(record)
                    if self.journal is not None:
                        self.journal.applied(index)
                        self.fault_injector.maybe_fail("after_operation_commit")
                    if index == 0:
                        self.fault_injector.maybe_fail("after_first_file_write")

        except BaseException as exc:
            # ----------------------------------------------
            # 4. partial failure → reverse recovery
            # ----------------------------------------------

            await self._notify("rollback_started", {"applied_operations": len(applied)})
            report = await self._recover(applied)
            if report.complete and self.journal is not None:
                self.journal.rolled_back()

            if not report.complete:
                await self._notify("rollback_failed", {
                    "attempted": report.attempted,
                    "recovered": report.recovered,
                    "failure_count": len(report.failures),
                })
                raise WorkspaceRecoveryError(
                    report=report,
                    primary_error=exc,
                    cause=str(exc),
                ) from exc

            await self._notify("rollback_completed", {
                "attempted": report.attempted,
                "recovered": report.recovered,
            })

            raise

        changed_files: list[str] = []

        for record in applied:
            changed_files.extend(record.changed_paths)

        return WorkspaceMutationResult(
            mutations=[
                record.mutation
                for record in applied
                if record.mutation is not None
            ],
            changed_files=changed_files,
        )

    def set_lifecycle_observer(self, observer) -> None:
        self.lifecycle_observer = observer

    async def _notify(self, stage: str, payload: dict) -> None:
        if self.lifecycle_observer is None:
            return
        try:
            await self.lifecycle_observer.on_mutation_event(stage=stage, payload=payload)
        except Exception:
            return

    # ------------------------------------------------------------------
    # apply 单个操作
    # ------------------------------------------------------------------

    async def _apply_one(
        self,
        index: int,
        operation,
    ) -> "_AppliedRecord":
        if isinstance(operation, PlannedCreate):
            return self._apply_create(index, operation)

        if isinstance(operation, PlannedEdit):
            return self._apply_edit(index, operation)

        if isinstance(operation, PlannedDelete):
            return self._apply_delete(index, operation)

        if isinstance(operation, PlannedMove):
            return self._apply_move(index, operation)

        raise AssertionError(  # pragma: no cover
            f"未知 planned 操作: {type(operation)}"
        )

    def _apply_create(
        self,
        index: int,
        operation: PlannedCreate,
    ) -> "_AppliedRecord":
        file_path = self.workspace.resolve(operation.path)

        # 最终 recheck：仍不存在。
        if file_path.exists():
            raise FileExistsError(operation.path)

        self.fault_injector.maybe_fail("checkpoint_create_failure")
        checkpoint = self.checkpoint_store.create(
            path=file_path,
            workspace_relative=operation.path,
        )

        new_bytes = encode_text_file(
            operation.content,
            newline="LF",
            has_bom=False,
        )

        atomic_write_bytes(file_path, new_bytes, fault_injector=self.fault_injector)

        new_version = version_of_file(file_path)

        mutation = MutationResult(
            path=operation.path,
            old_version=None,
            new_version=new_version,
            diff="",
            changed=True,
            checkpoint_id=checkpoint.id,
        )

        return _AppliedRecord(
            index=index,
            kind="create_file",
            changed_path=operation.path,
            mutation=mutation,
            checkpoint_id=checkpoint.id,
            expected_state=ExpectedFileState(
                exists=True,
                version=new_version,
            ),
        )

    def _apply_edit(
        self,
        index: int,
        operation: PlannedEdit,
    ) -> "_AppliedRecord":
        file_path = self.workspace.resolve(operation.path)

        # final recheck
        current = version_of_file(file_path)

        if not same_version(
            current,
            operation.snapshot.version,
        ):
            from coding_agent.errors import StaleVersionError

            raise StaleVersionError(
                expected=operation.snapshot.version.digest,
                current=current.digest,
            )

        self.fault_injector.maybe_fail("checkpoint_create_failure")
        checkpoint = self.checkpoint_store.create(
            path=file_path,
            workspace_relative=operation.path,
        )

        from coding_agent.filesystem.atomic_write import (
            atomic_write_bytes_checked,
        )

        new_bytes = encode_text_file(
            operation.new_content,
            newline=operation.snapshot.newline,
            has_bom=operation.snapshot.has_bom,
        )

        atomic_write_bytes_checked(
            file_path,
            new_bytes,
            expected_version=operation.snapshot.version,
            fault_injector=self.fault_injector,
        )

        new_version = version_of_file(file_path)

        mutation = MutationResult(
            path=operation.path,
            old_version=operation.snapshot.version,
            new_version=new_version,
            diff=operation.diff,
            changed_ranges=operation.changed_ranges,
            changed=True,
            checkpoint_id=checkpoint.id,
        )

        return _AppliedRecord(
            index=index,
            kind="edit_file",
            changed_path=operation.path,
            mutation=mutation,
            checkpoint_id=checkpoint.id,
            expected_state=ExpectedFileState(
                exists=True,
                version=new_version,
            ),
        )

    def _apply_delete(
        self,
        index: int,
        operation: PlannedDelete,
    ) -> "_AppliedRecord":
        file_path = self.workspace.resolve(operation.path)

        self.fault_injector.maybe_fail("checkpoint_create_failure")
        checkpoint = self.checkpoint_store.create(
            path=file_path,
            workspace_relative=operation.path,
        )

        old_version = delete_file_checked(
            file_path,
            expected_version=operation.snapshot.version,
        )

        mutation = MutationResult(
            path=operation.path,
            old_version=old_version,
            new_version=None,
            diff="",
            changed=True,
            checkpoint_id=checkpoint.id,
        )

        return _AppliedRecord(
            index=index,
            kind="delete_file",
            changed_path=operation.path,
            mutation=mutation,
            checkpoint_id=checkpoint.id,
            expected_state=ExpectedFileState(
                exists=False,
            ),
        )

    def _apply_move(
        self,
        index: int,
        operation: PlannedMove,
    ) -> "_AppliedRecord":
        source_path = self.workspace.resolve(operation.source)
        destination_path = self.workspace.resolve(
            operation.destination
        )

        self.fault_injector.maybe_fail("checkpoint_create_failure")
        source_checkpoint = self.checkpoint_store.create(
            path=source_path,
            workspace_relative=operation.source,
        )

        self.fault_injector.maybe_fail("checkpoint_create_failure")
        destination_checkpoint = self.checkpoint_store.create(
            path=destination_path,
            workspace_relative=operation.destination,
        )

        moved_version = move_file_checked(
            source_path,
            destination_path,
            expected_source_version=operation.source_snapshot.version,
        )

        mutation = MutationResult(
            path=operation.destination,
            old_version=operation.source_snapshot.version,
            new_version=moved_version,
            diff="",
            changed=True,
            checkpoint_id=source_checkpoint.id,
        )

        recovery_record = MoveRecoveryRecord(
            source=operation.source,
            destination=operation.destination,
            source_checkpoint_id=source_checkpoint.id,
            destination_checkpoint_id=destination_checkpoint.id,
            moved_version=moved_version,
        )

        return _AppliedRecord(
            index=index,
            kind="move_file",
            changed_path=operation.destination,
            mutation=mutation,
            checkpoint_id=source_checkpoint.id,
            move_record=recovery_record,
        )

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------

    async def _recover(
        self,
        applied: list["_AppliedRecord"],
    ) -> RecoveryReport:
        attempted = len(applied)
        recovered = 0
        failures: list[RecoveryFailure] = []

        for record in reversed(applied):
            try:
                await self._recover_one(record)
                recovered += 1
            except Exception as exc:
                failures.append(
                    RecoveryFailure(
                        operation_index=record.index,
                        paths=record.recovery_paths,
                        error_code=type(exc).__name__,
                        message=str(exc),
                    )
                )

        return RecoveryReport(
            attempted=attempted,
            recovered=recovered,
            failures=failures,
        )

    async def _recover_one(
        self,
        record: "_AppliedRecord",
    ) -> None:
        self.fault_injector.maybe_fail("rollback_failure")
        if record.kind == "move_file":
            assert record.move_record is not None
            await self.rollback_manager.rollback_move(
                record.move_record
            )
            return

        assert record.checkpoint_id is not None
        assert record.expected_state is not None

        await self.rollback_manager.rollback_state(
            record.checkpoint_id,
            expected_current_state=record.expected_state,
        )


class _AppliedRecord:
    """内部记录：一次已应用操作及其 recovery 信息。"""

    def __init__(
        self,
        *,
        index: int,
        kind: str,
        changed_path: str | None,
        mutation: MutationResult | None,
        checkpoint_id: str | None,
        expected_state: ExpectedFileState | None = None,
        move_record: MoveRecoveryRecord | None = None,
    ) -> None:
        self.index = index
        self.kind = kind
        self.changed_path = changed_path
        self.mutation = mutation
        self.checkpoint_id = checkpoint_id
        self.expected_state = expected_state
        self.move_record = move_record

    @property
    def changed_paths(self) -> list[str]:
        """返回该操作影响的所有 path（用于 VerificationScope）。

        move 返回 [source, destination]。
        """
        if self.move_record is not None:
            return [
                self.move_record.source,
                self.move_record.destination,
            ]

        if self.changed_path is not None:
            return [self.changed_path]

        return []

    @property
    def recovery_paths(self) -> list[str]:
        if self.move_record is not None:
            return [
                self.move_record.source,
                self.move_record.destination,
            ]

        return [self.changed_path or "<unknown>"]
