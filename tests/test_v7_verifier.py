"""V7 Stop Rule H: deterministic VerificationDecision."""

import asyncio
from pathlib import Path

from coding_agent.agents.verifier import Verifier
from coding_agent.diagnostics.models import Diagnostic, DiagnosticSeverity
from coding_agent.filesystem.workspace import Workspace
from coding_agent.verification.models import VerificationKind, VerificationResult
from coding_agent.verification.pipeline import VerificationPipeline


class _StaticVerifier:
    name = "pytest"
    kind = VerificationKind.TEST

    def __init__(self, result: VerificationResult) -> None:
        self.result = result
        self.calls = 0

    async def verify(self, *, workspace: Workspace) -> VerificationResult:
        self.calls += 1
        return self.result


def _run(coro):
    return asyncio.run(coro)


def test_verifier_passes_when_pipeline_is_green(tmp_path: Path):
    runtime = _StaticVerifier(
        VerificationResult(kind=VerificationKind.TEST, name="pytest", passed=True)
    )
    decision = _run(
        Verifier(VerificationPipeline([runtime])).verify(
            workspace=Workspace(tmp_path),
            acceptance_criteria=["Tests pass"],
        )
    )

    assert runtime.calls == 1
    assert decision.passed is True
    assert decision.failed_criteria == []
    assert decision.should_replan is False


def test_verifier_failure_marks_criteria_and_replan(tmp_path: Path):
    diagnostic = Diagnostic(
        path="tests/test_calc.py",
        line=3,
        severity=DiagnosticSeverity.ERROR,
        source="pytest",
        message="expected 3, got -1",
    )
    runtime = _StaticVerifier(
        VerificationResult(
            kind=VerificationKind.TEST,
            name="pytest",
            passed=False,
            diagnostics=[diagnostic],
        )
    )
    decision = _run(
        Verifier(VerificationPipeline([runtime])).verify(
            workspace=Workspace(tmp_path),
            acceptance_criteria=["Addition test passes"],
        )
    )

    assert decision.passed is False
    assert decision.failed_criteria == ["Addition test passes"]
    assert decision.should_replan is True
    assert "pytest" in decision.reason
    assert "new ERROR" in decision.reason
    assert decision.diagnostics == [diagnostic]


def test_existing_error_is_not_reported_as_new_but_failed_check_still_replans(
    tmp_path: Path,
):
    diagnostic = Diagnostic(
        severity=DiagnosticSeverity.ERROR,
        source="lint",
        message="existing",
    )
    runtime = _StaticVerifier(
        VerificationResult(
            kind=VerificationKind.LINT,
            name="lint",
            passed=False,
            diagnostics=[diagnostic],
        )
    )
    decision = _run(
        Verifier(VerificationPipeline([runtime])).verify(
            workspace=Workspace(tmp_path),
            acceptance_criteria=[],
            baseline_diagnostics=[diagnostic],
        )
    )

    assert decision.should_replan is True
    assert "new ERROR" not in decision.reason
    assert decision.failed_criteria == ["Verification pipeline must pass"]
