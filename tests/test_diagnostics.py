"""Tests for unified Diagnostic model and DiagnosticBundle."""

from coding_agent.diagnostics.models import (
    Diagnostic,
    DiagnosticBundle,
    DiagnosticSeverity,
)


def _diag(
    *,
    path="src/a.py",
    line=1,
    column=1,
    severity=DiagnosticSeverity.ERROR,
    source="pytest",
    code=None,
    message="err",
) -> Diagnostic:
    return Diagnostic(
        path=path,
        line=line,
        column=column,
        severity=severity,
        source=source,
        code=code,
        message=message,
    )


def test_diagnostic_path_workspace_relative():
    d = _diag(path="src/a.py")

    assert d.path == "src/a.py"
    assert "\\" not in d.path


def test_diagnostic_location_optional():
    d = Diagnostic(
        severity=DiagnosticSeverity.ERROR,
        source="pytest",
        message="boom",
    )

    assert d.path is None
    assert d.line is None
    assert d.column is None


def test_diagnostic_severity_enum():
    assert DiagnosticSeverity.ERROR.value == "error"
    assert DiagnosticSeverity.WARNING.value == "warning"
    assert DiagnosticSeverity.INFO.value == "info"
    assert DiagnosticSeverity.HINT.value == "hint"


def test_diagnostic_line_contract_is_one_based():
    d = _diag(line=1)

    # 内部约定 1-based。
    assert d.line == 1


# ---------------------------------------------------------------------------
# DiagnosticBundle
# ---------------------------------------------------------------------------


def test_diagnostics_sort():
    bundle = DiagnosticBundle(
        diagnostics=[
            _diag(path="b.py", line=2),
            _diag(path="a.py", line=10),
            _diag(path="a.py", line=1),
        ]
    )

    sorted_bundle = bundle.sorted()

    paths_lines = [
        (d.path, d.line)
        for d in sorted_bundle.diagnostics
    ]

    assert paths_lines == [
        ("a.py", 1),
        ("a.py", 10),
        ("b.py", 2),
    ]


def test_diagnostics_group_by_file():
    bundle = DiagnosticBundle(
        diagnostics=[
            _diag(path="a.py"),
            _diag(path="a.py", line=2),
            _diag(path="b.py"),
        ]
    )

    grouped = bundle.group_by_file()

    assert len(grouped["a.py"]) == 2
    assert len(grouped["b.py"]) == 1


def test_diagnostics_exact_dedup():
    bundle = DiagnosticBundle(
        diagnostics=[
            _diag(path="a.py", line=1),
            _diag(path="a.py", line=1),
            _diag(path="a.py", line=2),
        ]
    )

    deduped = bundle.deduplicate()

    assert deduped.count == 2


def test_diagnostics_keep_different_sources():
    bundle = DiagnosticBundle(
        diagnostics=[
            _diag(path="a.py", line=1, source="pytest", message="x"),
            _diag(path="a.py", line=1, source="ruff", message="x"),
        ]
    )

    deduped = bundle.deduplicate()

    # 不同 source 的相似诊断不合并。
    assert deduped.count == 2


def test_diagnostics_limit():
    bundle = DiagnosticBundle(
        diagnostics=[
            _diag(path="a.py", line=i)
            for i in range(1000)
        ]
    )

    limited = bundle.limit(max_diagnostics=200)

    assert limited.count == 200


def test_diagnostic_message_truncation():
    bundle = DiagnosticBundle(
        diagnostics=[
            _diag(message="x" * 5000)
        ]
    )

    limited = bundle.limit(max_message_chars=2048)

    message = limited.diagnostics[0].message

    assert len(message) <= 2048 + len("... [message truncated]")
    assert "... [message truncated]" in message
