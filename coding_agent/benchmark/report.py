"""JSON, CSV and Markdown benchmark reporting."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path

from coding_agent.benchmark.models import BenchmarkResult


def summarize(results: list[BenchmarkResult]) -> dict:
    summary = _summarize_group(results)
    profiles = sorted({item.profile for item in results})
    summary["by_profile"] = {
        profile: _summarize_group([
            item for item in results if item.profile == profile
        ])
        for profile in profiles
    }
    return summary


def _summarize_group(results: list[BenchmarkResult]) -> dict:
    count = len(results)
    recovery = [item for item in results if item.recovery_attempted]
    total = lambda field: sum(getattr(item, field) for item in results)
    mean = lambda field: (total(field) / count if count else 0.0)
    return {
        "tasks": count,
        "correctness": {
            "success": sum(item.success for item in results),
            "verification_passed": sum(item.verification_passed for item in results),
            "task_success_rate": (sum(item.success for item in results) / count if count else 0.0),
        },
        "safety": {
            "security_violations": total("security_violations"),
            "sandbox_violations": total("sandbox_violations"),
            "parser_fallbacks": total("parser_fallbacks"),
            "security_violation_rate": (
                sum(item.security_violations > 0 for item in results) / count if count else 0.0
            ),
        },
        "efficiency": {
            "mean_model_calls": mean("model_calls"),
            "mean_tool_calls": mean("tool_calls"),
            "mean_tokens": mean("input_tokens") + mean("output_tokens"),
            "mean_duration_ms": mean("duration_ms"),
            "replan_rate": mean("replans"),
            "no_match_rate": mean("no_match_errors"),
            "stale_rate": mean("stale_errors"),
            "mutation_failure_rate": mean("mutation_failures"),
            "mean_policy_asks": mean("policy_asks"),
            "mean_approval_interruptions": mean("approval_interruptions"),
            "mean_auto_classifier_calls": mean("auto_classifier_calls"),
            "mean_auto_classifier_latency_ms": mean("auto_classifier_latency_ms"),
            "mean_auto_classifier_input_tokens": mean("auto_classifier_input_tokens"),
            "mean_auto_classifier_output_tokens": mean("auto_classifier_output_tokens"),
            "auto_allow_rate": (
                total("auto_allows") / total("auto_classifier_calls")
                if total("auto_classifier_calls") else 0.0
            ),
            "auto_block_rate": (
                total("auto_blocks") / total("auto_classifier_calls")
                if total("auto_classifier_calls") else 0.0
            ),
        },
        "recovery": {
            "attempted": len(recovery),
            "succeeded": sum(item.recovery_succeeded for item in recovery),
            "success_rate": (
                sum(item.recovery_succeeded for item in recovery) / len(recovery)
                if recovery else 0.0
            ),
        },
    }


def write_reports(results: list[BenchmarkResult], output_directory: Path) -> dict[str, Path]:
    output_directory = output_directory.resolve(strict=False)
    output_directory.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    json_path = output_directory / "benchmark.json"
    csv_path = output_directory / "benchmark.csv"
    markdown_path = output_directory / "benchmark.md"
    _atomic_text(json_path, json.dumps({
        "summary": summary,
        "results": [item.model_dump(mode="json") for item in results],
    }, ensure_ascii=False, indent=2))

    csv_buffer = io.StringIO(newline="")
    fields = list(BenchmarkResult.model_fields)
    writer = csv.DictWriter(csv_buffer, fieldnames=fields)
    writer.writeheader()
    for item in results:
        writer.writerow(item.model_dump(mode="json"))
    _atomic_text(csv_path, csv_buffer.getvalue())

    correctness = summary["correctness"]
    safety = summary["safety"]
    efficiency = summary["efficiency"]
    recovery = summary["recovery"]
    profile_lines = "".join(
        f"- {profile}: success "
        f"{values['correctness']['success']}/{values['tasks']}, "
        f"violations {values['safety']['security_violations']}, "
        f"approval interruptions "
        f"{values['efficiency']['mean_approval_interruptions']:.2f}, "
        f"AUTO allow rate {values['efficiency']['auto_allow_rate']:.4f}\n"
        for profile, values in summary["by_profile"].items()
    )
    markdown = (
        "# V10 Benchmark Report\n\n"
        "## Correctness\n\n"
        f"- Tasks: {summary['tasks']}\n- Success: {correctness['success']}/{summary['tasks']}\n"
        f"- Verification passed: {correctness['verification_passed']}/{summary['tasks']}\n\n"
        "## Safety\n\n"
        f"- Security violations: {safety['security_violations']}\n\n"
        f"- Sandbox violations: {safety['sandbox_violations']}\n"
        f"- Parser fallbacks: {safety['parser_fallbacks']}\n\n"
        "## Efficiency\n\n"
        f"- Average model calls: {efficiency['mean_model_calls']:.2f}\n"
        f"- Average tool calls: {efficiency['mean_tool_calls']:.2f}\n"
        f"- Average tokens: {efficiency['mean_tokens']:.2f}\n"
        f"- Average duration: {efficiency['mean_duration_ms'] / 1000:.3f}s\n\n"
        f"- Average policy asks: {efficiency['mean_policy_asks']:.2f}\n"
        f"- Average approval interruptions: {efficiency['mean_approval_interruptions']:.2f}\n"
        f"- Average AUTO classifier calls: {efficiency['mean_auto_classifier_calls']:.2f}\n"
        f"- AUTO allow rate: {efficiency['auto_allow_rate']:.4f}\n"
        f"- AUTO block rate: {efficiency['auto_block_rate']:.4f}\n\n"
        f"- Average AUTO classifier latency: "
        f"{efficiency['mean_auto_classifier_latency_ms']:.2f}ms\n"
        f"- Average AUTO classifier estimated input tokens: "
        f"{efficiency['mean_auto_classifier_input_tokens']:.2f}\n"
        f"- Average AUTO classifier estimated output tokens: "
        f"{efficiency['mean_auto_classifier_output_tokens']:.2f}\n\n"
        "## V8 / V9 / V10 Profiles\n\n"
        f"{profile_lines}\n"
        "## Recovery\n\n"
        f"- Recovery success: {recovery['succeeded']}/{recovery['attempted']}\n"
    )
    _atomic_text(markdown_path, markdown)
    return {"json": json_path, "csv": csv_path, "markdown": markdown_path}


def _atomic_text(destination: Path, content: str) -> None:
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.remove(temporary)
