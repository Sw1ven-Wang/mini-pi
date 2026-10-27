"""Deterministic V7 verification decision over the existing pipeline."""

from __future__ import annotations

from coding_agent.agents.models import VerificationDecision
from coding_agent.diagnostics.models import Diagnostic, DiagnosticSeverity
from coding_agent.filesystem.workspace import Workspace
from coding_agent.verification.models import VerificationScope
from coding_agent.verification.pipeline import VerificationPipeline


class Verifier:
    """Translate concrete verification results into a replan decision."""

    def __init__(self, pipeline: VerificationPipeline) -> None:
        self.pipeline = pipeline

    async def verify(
        self,
        *,
        workspace: Workspace,
        acceptance_criteria: list[str],
        scope: VerificationScope | None = None,
        baseline_diagnostics: list[Diagnostic] | None = None,
    ) -> VerificationDecision:
        summary = await self.pipeline.run(workspace=workspace, scope=scope)
        baseline = {_fingerprint(item) for item in (baseline_diagnostics or [])}
        new_errors = [
            item
            for item in summary.diagnostics
            if item.severity == DiagnosticSeverity.ERROR
            and _fingerprint(item) not in baseline
        ]
        failed_results = [result.name for result in summary.results if not result.passed]
        passed = summary.passed and not new_errors

        if passed:
            return VerificationDecision(
                passed=True,
                failed_criteria=[],
                diagnostics=summary.diagnostics,
                should_replan=False,
                reason="All verification checks passed with no new ERROR diagnostics.",
            )

        failed_criteria = list(acceptance_criteria) or [
            "Verification pipeline must pass"
        ]
        reasons: list[str] = []
        if failed_results:
            reasons.append("failed checks: " + ", ".join(failed_results))
        if new_errors:
            reasons.append(f"new ERROR diagnostics: {len(new_errors)}")

        return VerificationDecision(
            passed=False,
            failed_criteria=failed_criteria,
            diagnostics=summary.diagnostics,
            should_replan=True,
            reason="; ".join(reasons) or "Acceptance criteria were not satisfied.",
        )


def _fingerprint(diagnostic: Diagnostic) -> tuple:
    return (
        diagnostic.path,
        diagnostic.line,
        diagnostic.column,
        diagnostic.severity,
        diagnostic.source,
        diagnostic.code,
        diagnostic.message,
    )
