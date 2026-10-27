"""VerificationPipeline: 串行执行多个 verifier 并聚合诊断。"""

from __future__ import annotations

from coding_agent.diagnostics.models import DiagnosticBundle
from coding_agent.filesystem.workspace import Workspace
from coding_agent.verification.base import Verifier
from coding_agent.verification.models import (
    VerificationResult,
    VerificationScope,
    VerificationSummary,
)
from coding_agent.testing.fault_injection import NoopFaultInjector

# 诊断聚合上限。
DEFAULT_MAX_DIAGNOSTICS = 200
DEFAULT_MAX_MESSAGE_CHARS = 2048


class VerificationPipeline:
    """按给定顺序串行执行 verifier。"""

    def __init__(
        self,
        verifiers: list[Verifier],
        fault_injector=None,
    ) -> None:
        self.verifiers = verifiers
        self.fault_injector = fault_injector or NoopFaultInjector()

    async def run(
        self,
        *,
        workspace: Workspace,
        scope: VerificationScope | None = None,
        stop_on_failure: bool = False,
        max_diagnostics: int = DEFAULT_MAX_DIAGNOSTICS,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
    ) -> VerificationSummary:
        results: list[VerificationResult] = []

        for verifier in self.verifiers:
            self.fault_injector.maybe_fail("verification_failure")
            result = await verifier.verify(
                workspace=workspace,
            )

            results.append(result)

            if stop_on_failure and not result.passed:
                break

        # 聚合所有 diagnostics：sort + dedup + limit。
        bundle = DiagnosticBundle(
            diagnostics=[
                diagnostic
                for result in results
                for diagnostic in result.diagnostics
            ]
        )

        bundle = (
            bundle
            .sorted()
            .deduplicate()
            .limit(
                max_diagnostics=max_diagnostics,
                max_message_chars=max_message_chars,
            )
        )

        return VerificationSummary(
            passed=bool(results) and all(
                result.passed
                for result in results
            ),
            results=results,
            diagnostics=bundle.diagnostics,
        )
