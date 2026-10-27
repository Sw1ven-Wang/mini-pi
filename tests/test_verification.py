"""Tests for verification models, parsers, verifiers, and pipeline."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.diagnostics.models import (
    Diagnostic,
    DiagnosticSeverity,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import powershell_config
from coding_agent.verification.base import Verifier
from coding_agent.verification.command import CommandVerifier
from coding_agent.verification.lint_parser import RuffParser
from coding_agent.verification.models import (
    VerificationKind,
    VerificationResult,
    VerificationScope,
)
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.pytest_parser import PytestParser


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# VerificationResult model
# ---------------------------------------------------------------------------


def test_verification_result_model():
    result = VerificationResult(
        kind=VerificationKind.TEST,
        name="pytest",
        passed=True,
        exit_code=0,
        diagnostics=[],
    )

    assert result.kind == VerificationKind.TEST
    assert result.passed is True
    assert result.exit_code == 0
    assert result.diagnostics == []


# ---------------------------------------------------------------------------
# Pytest parser（静态字符串 fixture，不依赖 subprocess）
# ---------------------------------------------------------------------------


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_pytest_parser_assertion(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x")

    parser = PytestParser()

    stdout = (
        "tests/test_calc.py:8: AssertionError\n"
        "=============================== 1 failed ======\n"
    )

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert len(diagnostics) == 1

    d = diagnostics[0]
    assert d.path == "tests/test_calc.py"
    assert d.line == 8
    assert d.severity == DiagnosticSeverity.ERROR
    assert d.source == "pytest"


def test_pytest_parser_multiple_failures(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x")
    (tmp_path / "tests" / "test_b.py").write_text("x")

    parser = PytestParser()

    stdout = (
        "tests/test_a.py:3: AssertionError\n"
        "tests/test_b.py:5: AssertionError\n"
    )

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert len(diagnostics) == 2
    assert {d.path for d in diagnostics} == {
        "tests/test_a.py",
        "tests/test_b.py",
    }


def test_pytest_parser_windows_path(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("x")

    parser = PytestParser()

    stdout = "tests\\test_calc.py:8: AssertionError\n"

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].path == "tests/test_calc.py"
    assert "\\" not in diagnostics[0].path


def test_pytest_parser_no_location(tmp_path: Path):
    parser = PytestParser()

    stdout = "AssertionError: assert 4 == 5\n"

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert len(diagnostics) == 1
    assert diagnostics[0].path is None
    assert diagnostics[0].line is None


def test_pytest_parser_malformed_output_best_effort(tmp_path: Path):
    parser = PytestParser()

    stdout = "garbage that is not parseable\n" * 5

    # 不得 crash，返回空列表。
    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert diagnostics == []


# ---------------------------------------------------------------------------
# Ruff parser（结构化 JSON fixture）
# ---------------------------------------------------------------------------


def _ruff_json_entry(path, row, column, code, message):
    return {
        "filename": path,
        "location": {"row": row, "column": column},
        "code": code,
        "message": message,
    }


def test_ruff_parser_single_diagnostic(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x")

    parser = RuffParser()

    import json

    stdout = json.dumps(
        [
            _ruff_json_entry(
                "src/calc.py",
                1,
                8,
                "F401",
                "`os` imported but unused",
            )
        ]
    )

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert len(diagnostics) == 1

    d = diagnostics[0]
    assert d.path == "src/calc.py"
    assert d.line == 1
    assert d.column == 8
    assert d.code == "F401"
    assert d.source == "ruff"
    assert "unused" in d.message


def test_ruff_parser_multiple_diagnostics(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x")

    parser = RuffParser()

    import json

    stdout = json.dumps(
        [
            _ruff_json_entry("src/calc.py", 1, 8, "F401", "a"),
            _ruff_json_entry("src/calc.py", 2, 1, "E501", "b"),
        ]
    )

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert len(diagnostics) == 2


def test_ruff_parser_path_conversion(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x")

    parser = RuffParser()

    import json

    stdout = json.dumps(
        [_ruff_json_entry("src\\calc.py", 1, 1, "F401", "a")]
    )

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert diagnostics[0].path == "src/calc.py"


def test_ruff_parser_code_and_message(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x")

    parser = RuffParser()

    import json

    stdout = json.dumps(
        [
            _ruff_json_entry(
                "src/calc.py",
                3,
                4,
                "E501",
                "Line too long",
            )
        ]
    )

    diagnostics = parser.parse(
        stdout=stdout,
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert diagnostics[0].code == "E501"
    assert diagnostics[0].message == "Line too long"


def test_ruff_parser_malformed_json(tmp_path: Path):
    parser = RuffParser()

    diagnostics = parser.parse(
        stdout="not json at all",
        stderr="",
        workspace=_workspace(tmp_path),
    )

    assert diagnostics == []


# ---------------------------------------------------------------------------
# CommandVerifier
# ---------------------------------------------------------------------------


def test_command_verifier_pass(tmp_path: Path):
    runner = ProcessRunner()
    verifier = CommandVerifier(
        name="echo",
        kind=VerificationKind.CUSTOM,
        command='Write-Output "OK"',
        process_runner=runner,
        shell_config=powershell_config(),
    )

    result = _run(
        verifier.verify(workspace=Workspace(tmp_path))
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert result.duration_ms is not None


def test_command_verifier_failure(tmp_path: Path):
    runner = ProcessRunner()
    verifier = CommandVerifier(
        name="fail",
        kind=VerificationKind.CUSTOM,
        command="exit 1",
        process_runner=runner,
        shell_config=powershell_config(),
    )

    result = _run(
        verifier.verify(workspace=Workspace(tmp_path))
    )

    assert result.passed is False
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Pipeline（用 fake verifier，不依赖 subprocess）
# ---------------------------------------------------------------------------


class _FakeVerifier:
    def __init__(
        self,
        name: str,
        passed: bool,
        diagnostics=None,
    ):
        self.name = name
        self.kind = VerificationKind.CUSTOM
        self._passed = passed
        self._diagnostics = diagnostics or []
        self.order: list[str] = []

    async def verify(self, *, workspace):
        self.order.append(self.name)
        return VerificationResult(
            kind=self.kind,
            name=self.name,
            passed=self._passed,
            diagnostics=self._diagnostics,
        )


def test_pipeline_all_pass(tmp_path: Path):
    pipeline = VerificationPipeline(
        [
            _FakeVerifier("a", True),
            _FakeVerifier("b", True),
        ]
    )

    summary = _run(
        pipeline.run(workspace=Workspace(tmp_path))
    )

    assert summary.passed is True
    assert len(summary.results) == 2


def test_pipeline_test_failure(tmp_path: Path):
    pipeline = VerificationPipeline(
        [
            _FakeVerifier("format", True),
            _FakeVerifier("pytest", False),
        ]
    )

    summary = _run(
        pipeline.run(workspace=Workspace(tmp_path))
    )

    assert summary.passed is False


def test_pipeline_lint_failure(tmp_path: Path):
    pipeline = VerificationPipeline(
        [
            _FakeVerifier("ruff", False, diagnostics=[
                Diagnostic(
                    path="src/a.py",
                    line=1,
                    severity=DiagnosticSeverity.WARNING,
                    source="ruff",
                    code="F401",
                    message="unused",
                )
            ]),
        ]
    )

    summary = _run(
        pipeline.run(workspace=Workspace(tmp_path))
    )

    assert summary.passed is False
    assert len(summary.diagnostics) == 1
    assert summary.diagnostics[0].code == "F401"


def test_pipeline_multiple_diagnostics(tmp_path: Path):
    pipeline = VerificationPipeline(
        [
            _FakeVerifier("a", True, diagnostics=[
                Diagnostic(
                    path="a.py", line=1,
                    severity=DiagnosticSeverity.ERROR,
                    source="x", message="e1",
                )
            ]),
            _FakeVerifier("b", True, diagnostics=[
                Diagnostic(
                    path="b.py", line=1,
                    severity=DiagnosticSeverity.ERROR,
                    source="y", message="e2",
                )
            ]),
        ]
    )

    summary = _run(
        pipeline.run(workspace=Workspace(tmp_path))
    )

    assert len(summary.diagnostics) == 2


def test_pipeline_serial_order(tmp_path: Path):
    verifiers = [
        _FakeVerifier("format", True),
        _FakeVerifier("lint", True),
        _FakeVerifier("tests", True),
    ]

    pipeline = VerificationPipeline(verifiers)

    _run(pipeline.run(workspace=Workspace(tmp_path)))

    # 串行顺序执行。
    assert verifiers[0].order == ["format"]
    assert verifiers[1].order == ["lint"]
    assert verifiers[2].order == ["tests"]


def test_pipeline_stop_on_failure(tmp_path: Path):
    verifiers = [
        _FakeVerifier("a", False),
        _FakeVerifier("b", True),
    ]

    pipeline = VerificationPipeline(verifiers)

    summary = _run(
        pipeline.run(
            workspace=Workspace(tmp_path),
            stop_on_failure=True,
        )
    )

    # stop_on_failure：a 失败后不执行 b。
    assert len(summary.results) == 1
    assert verifiers[1].order == []


def test_pipeline_continue_on_failure(tmp_path: Path):
    verifiers = [
        _FakeVerifier("a", False),
        _FakeVerifier("b", True),
    ]

    pipeline = VerificationPipeline(verifiers)

    summary = _run(
        pipeline.run(
            workspace=Workspace(tmp_path),
            stop_on_failure=False,
        )
    )

    assert len(summary.results) == 2


def test_pipeline_timeout(tmp_path: Path):
    """verifier timeout 由 CommandVerifier 的 ProcessRunner 处理。"""
    runner = ProcessRunner()
    verifier = CommandVerifier(
        name="slow",
        kind=VerificationKind.CUSTOM,
        command="Start-Sleep -Seconds 60",
        process_runner=runner,
        shell_config=powershell_config(),
        timeout_seconds=1,
    )

    pipeline = VerificationPipeline([verifier])

    summary = _run(
        pipeline.run(workspace=Workspace(tmp_path))
    )

    # timeout → exit_code != 0 → passed False。
    assert summary.passed is False


def test_pipeline_process_start_failed(tmp_path: Path):
    """verifier 命令无法启动时，ProcessRunner 抛 ProcessStartFailedError。"""
    from coding_agent.errors import ProcessStartFailedError
    from coding_agent.process.shell import ShellConfig, ShellType

    runner = ProcessRunner()
    verifier = CommandVerifier(
        name="broken",
        kind=VerificationKind.CUSTOM,
        command="echo hi",
        process_runner=runner,
        shell_config=ShellConfig(
            shell_type=ShellType.POWERSHELL,
            executable="definitely-not-a-real-exe-xyz",
            args=("-Command",),
        ),
    )

    with pytest.raises(ProcessStartFailedError):
        _run(
            verifier.verify(workspace=Workspace(tmp_path))
        )
