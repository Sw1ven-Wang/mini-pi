import json
from pathlib import Path

from coding_agent.benchmark.models import BenchmarkExecution, BenchmarkTask
from coding_agent.benchmark.report import summarize, write_reports
from coding_agent.benchmark.runner import BenchmarkRunner
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.workspace_ir import EditFileOp, TextReplacement, WorkspaceChangeSet
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.mutation.runtime import WorkspaceMutationRuntime
from coding_agent.observability.metrics import RunMetrics
from coding_agent.safety.command_policy import CommandDecisionType, CommandPolicy
from coding_agent.testing.fault_injection import FaultInjectedError, ScriptedFaultInjector


FIXTURES = Path(__file__).parent / "fixtures"


def _tasks():
    return [
        BenchmarkTask.model_validate(item)
        for item in json.loads((FIXTURES / "benchmark_tasks.json").read_text())
    ]


async def _executor(task, workspace, recorder):
    metrics = RunMetrics(model_calls=1, tool_calls=2, input_tokens=40, output_tokens=20)
    if task.task_id == "partial_failure_recovery":
        injector = ScriptedFaultInjector({"after_first_file_write": 1})
        runtime = WorkspaceMutationRuntime(
            workspace, FileMutationQueue(), CheckpointStore(), injector
        )
        try:
            await runtime.apply(WorkspaceChangeSet(operations=[
                EditFileOp(
                    path="input.txt", expected_version=version_of_file(workspace.resolve("input.txt")),
                    replacements=[TextReplacement(old_text="broken", new_text="fixed")],
                )
            ]))
        except FaultInjectedError:
            pass
        metrics.mutation_attempts = 1
        metrics.mutation_failures = 1
        return BenchmarkExecution(
            success=True, verification_passed=True, metrics=metrics,
            recovery_attempted=True, recovery_succeeded=True,
        )
    if task.task_id in {"permission_deny", "dangerous_shell"}:
        decision = CommandPolicy().evaluate("Remove-Item input.txt")
        assert decision.decision == CommandDecisionType.DENY
        metrics.command_policy_blocks = 1
        return BenchmarkExecution(success=True, verification_passed=True, metrics=metrics)

    target = workspace.resolve("input.txt")
    change_set = WorkspaceChangeSet(operations=[EditFileOp(
        path="input.txt", expected_version=version_of_file(target),
        replacements=[TextReplacement(old_text="broken", new_text="fixed")],
    )])
    recorder.record_action(change_set, safe_arguments={"path": "input.txt"})
    runtime = WorkspaceMutationRuntime(workspace, FileMutationQueue(), CheckpointStore())
    result = await runtime.apply(change_set)
    metrics.mutation_attempts = 1
    if task.task_id == "stale_file":
        metrics.stale_version_errors = 1
        metrics.replans = 1
    if task.task_id == "no_match_replan":
        metrics.no_match_errors = 1
        metrics.replans = 1
    if task.task_id == "long_context":
        metrics.compactions = 1
    return BenchmarkExecution(
        success=True, verification_passed=True,
        changed_files=result.changed_files, metrics=metrics,
    )


async def test_offline_benchmark_runs_13_tasks_and_writes_all_reports(tmp_path: Path):
    tasks = _tasks()
    assert len(tasks) == 13
    runner = BenchmarkRunner(
        fixtures_root=FIXTURES / "repos", trace_directory=tmp_path / "traces"
    )
    results = await runner.run(tasks, executor=_executor)
    assert len(results) == 13
    assert all(item.success for item in results)
    assert sum(item.security_violations for item in results) == 0
    assert all((tmp_path / "traces" / f"{item.task_id}.json").exists() for item in results)
    summary = summarize(results)
    assert summary["correctness"]["success"] == 13
    assert summary["by_profile"]["V10"]["correctness"]["success"] == 13
    assert summary["recovery"] == {"attempted": 1, "succeeded": 1, "success_rate": 1.0}
    reports = write_reports(results, tmp_path / "reports")
    assert set(reports) == {"json", "csv", "markdown"}
    assert all(path.exists() for path in reports.values())
    assert "## Safety" in reports["markdown"].read_text(encoding="utf-8")
    assert "## V8 / V9 / V10 Profiles" in reports["markdown"].read_text(encoding="utf-8")


async def test_benchmark_rejects_unsafe_host_acceptance_and_cannot_fake_success(tmp_path: Path):
    task = BenchmarkTask(
        task_id="unsafe", repo_fixture="basic", prompt="x",
        acceptance_commands=["Remove-Item protected.txt"],
        forbidden_paths=["protected.txt"],
    )
    runner = BenchmarkRunner(fixtures_root=FIXTURES / "repos")

    async def claims_success(task, workspace, recorder):
        return BenchmarkExecution(success=True, verification_passed=True)

    result = await runner.run_task(task, executor=claims_success)
    assert result.success is False
    assert result.verification_passed is False
    assert result.security_violations == 1
