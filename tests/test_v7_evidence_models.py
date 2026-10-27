"""V7 Stop Rule C: version-bound evidence contracts."""

import pytest
from pydantic import ValidationError

from coding_agent.agents.models import EvidenceBundle, EvidenceSnippet
from coding_agent.filesystem.versions import FileVersion
from coding_agent.search.models import SearchMatch


def _version() -> FileVersion:
    return FileVersion(digest="a" * 64, size_bytes=12, mtime_ns=123)


def test_evidence_snippet_binds_content_to_version():
    snippet = EvidenceSnippet(
        path=r"src\math.py",
        start_line=2,
        end_line=3,
        content="def add(a, b):\n    return a + b",
        version=_version(),
    )

    assert snippet.path == "src/math.py"
    assert snippet.version.digest == "a" * 64


def test_evidence_snippet_requires_version():
    with pytest.raises(ValidationError):
        EvidenceSnippet(path="src/math.py", content="return 1")


@pytest.mark.parametrize(
    "path",
    [r"C:\outside.py", "/outside.py", "../outside.py", "src/../../outside.py"],
)
def test_evidence_path_must_be_workspace_relative(path: str):
    with pytest.raises(ValidationError):
        EvidenceSnippet(path=path, content="x", version=_version())


@pytest.mark.parametrize(
    ("start_line", "end_line"),
    [(1, None), (None, 1), (4, 3), (0, 1)],
)
def test_evidence_line_range_is_consistent(start_line, end_line):
    with pytest.raises(ValidationError):
        EvidenceSnippet(
            path="src/math.py",
            start_line=start_line,
            end_line=end_line,
            content="x",
            version=_version(),
        )


def test_evidence_bundle_has_the_required_four_channels():
    snippet = EvidenceSnippet(
        path="src/math.py",
        content="",
        version=_version(),
    )
    search_match = SearchMatch(
        path="src/math.py",
        line=1,
        column=1,
        preview="def add",
    )

    bundle = EvidenceBundle(
        snippets=[snippet],
        search_results=[search_match],
        repository_notes=["math.py defines add"],
        unresolved_questions=["Which callers rely on legacy behavior?"],
    )

    assert bundle.snippets == [snippet]
    assert bundle.search_results == [search_match]
    assert len(bundle.repository_notes) == 1
    assert len(bundle.unresolved_questions) == 1
