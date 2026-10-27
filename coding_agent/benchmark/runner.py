"""Runs benchmark tasks only against temporary fixture copies."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from coding_agent.benchmark.models import BenchmarkExecution, BenchmarkResult, BenchmarkTask
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.events import AgentEventType
from coding_agent.observability.tracing import TraceContext
from coding_agent.replay.recorder import TraceRecorder
from coding_agent.safety.command_analysis import CommandRisk, analyze_command
from coding_agent.safety.shell_ir import ShellKind
from coding_agent.sandbox.local import LocalSandbox
from coding_agent.process.shell import ShellType, default_shell_config


BenchmarkExecutor = Callable[
    [BenchmarkTask, Workspace, TraceRecorder], Awaitable[BenchmarkExecution]
]


class BenchmarkRunner:
    def __init__(self, *, fixtures_root: Path, trace_directory: Path | None = None) -> None:
        self.fixtures_root = fixtures_root.resolve(strict=True)
        self.trace_directory = (
            trace_directory.resolve(strict=False) if trace_directory is not None else None
        )

    async def run_task(
        self, task: BenchmarkTask, *, executor: BenchmarkExecutor
    ) -> BenchmarkResult:
        fixture = (self.fixtures_root / task.repo_fixture).resolve(strict=True)
        try:
            fixture.relative_to(self.fixtures_root)
        except ValueError as exc:
            raise ValueError("benchmark fixture 逃逸 fixtures_root") from exc
        if not fixture.is_dir():
            raise ValueError("benchmark fixture 必须是目录")

        started = perf_counter()
        trace = TraceContext.root()
        recorder = TraceRecorder()
        session_id = f"benchmark-{task.task_id}"
        execution = BenchmarkExecution(success=False)
        error: str | None = None
        violations = 0

        with tempfile.TemporaryDirectory(prefix="mini-pi-benchmark-") as temp:
            destination = Path(temp) / "workspace"
            shutil.copytree(fixture, destination, symlinks=True)
            workspace = Workspace(destination)
            before = _file_hashes(destination)
            await recorder.emit(trace.event(
                session_id=session_id,
                type=AgentEventType.TASK_STARTED,
                payload={"task_id": task.task_id, "max_turns": task.max_turns},
            ))
            try:
                execution = await executor(task, workspace, recorder)
                host_passed, policy_violations = await self._host_acceptance(
                    task.acceptance_commands, workspace
                )
                violations += policy_violations
                execution.verification_passed = (
                    execution.verification_passed and host_passed
                )
            except Exception as exc:
                error = type(exc).__name__

            after = _file_hashes(destination)
            for path in task.forbidden_paths:
                if before.get(path) != after.get(path):
                    violations += 1
            actual_changed = sorted(
                path for path in set(before) | set(after) if before.get(path) != after.get(path)
            )
            if task.expected_changed_files is not None:
                if actual_changed != sorted(task.expected_changed_files):
                    violations += 1

        success = (
            execution.success
            and execution.verification_passed
            and violations == 0
            and error is None
        )
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        terminal = AgentEventType.TASK_COMPLETED if success else AgentEventType.TASK_FAILED
        await recorder.emit(trace.event(
            session_id=session_id,
            type=terminal,
            payload={"passed": success, "duration_ms": duration_ms, "error": error},
        ))
        metrics = execution.metrics
        record = recorder.finalize(
            trace_id=trace.trace_id, session_id=session_id, metrics=metrics
        )
        if self.trace_directory is not None:
            TraceRecorder.save(record, self.trace_directory / f"{task.task_id}.json")
        return BenchmarkResult(
            task_id=task.task_id,
            profile=task.profile,
            success=success,
            verification_passed=execution.verification_passed,
            security_violations=violations,
            duration_ms=duration_ms,
            model_calls=metrics.model_calls,
            tool_calls=metrics.tool_calls,
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            mutation_attempts=metrics.mutation_attempts,
            mutation_failures=metrics.mutation_failures,
            replans=metrics.replans,
            stale_errors=metrics.stale_version_errors,
            no_match_errors=metrics.no_match_errors,
            recovery_attempted=execution.recovery_attempted,
            recovery_succeeded=execution.recovery_succeeded,
            trace_id=trace.trace_id,
            error=error,
            policy_asks=metrics.policy_asks,
            approval_interruptions=metrics.approval_interruptions,
            auto_classifier_calls=metrics.auto_classifier_calls,
            auto_allows=metrics.auto_allows,
            auto_blocks=metrics.auto_blocks,
            parser_fallbacks=metrics.parser_fallbacks,
            sandbox_violations=metrics.sandbox_violations,
            auto_classifier_latency_ms=metrics.auto_classifier_latency_ms,
            auto_classifier_input_tokens=metrics.auto_classifier_input_tokens,
            auto_classifier_output_tokens=metrics.auto_classifier_output_tokens,
        )

    async def run(
        self, tasks: list[BenchmarkTask], *, executor: BenchmarkExecutor
    ) -> list[BenchmarkResult]:
        results: list[BenchmarkResult] = []
        for task in tasks:
            results.append(await self.run_task(task, executor=executor))
        return results

    @staticmethod
    async def _host_acceptance(
        commands: list[str], workspace: Workspace
    ) -> tuple[bool, int]:
        violations = 0
        shell_config = default_shell_config()
        shell = (
            ShellKind.POWERSHELL
            if shell_config.shell_type == ShellType.POWERSHELL
            else ShellKind.BASH
        )
        sandbox = LocalSandbox(shell_config=shell_config)
        session = await sandbox.start(workspace=workspace)
        try:
            for command in commands:
                analysis = analyze_command(command, shell=shell)
                if analysis.risk not in {CommandRisk.READ_ONLY, CommandRisk.BUILD_TEST}:
                    violations += 1
                    return False, violations
                result = await session.run(command, timeout_seconds=60)
                if result.timed_out or result.exit_code != 0:
                    return False, violations
            return True, violations
        finally:
            await session.stop()


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
        if not any(
            part in {".git", ".pytest_cache", "__pycache__", ".mini_pi", ".venv"}
            for part in path.relative_to(root).parts
        )
    }
