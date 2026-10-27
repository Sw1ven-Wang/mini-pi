"""V7 Stop Rule F: isolated EditorRequest contract."""

from coding_agent.agents.models import (
    ChangeIntent,
    EditorRequest,
    EvidenceBundle,
    EvidenceSnippet,
)
from coding_agent.filesystem.versions import FileVersion


def test_editor_request_contains_only_intent_and_evidence():
    request = EditorRequest(
        intent=ChangeIntent(
            objective="Fix subtraction typo",
            target_files=["src/calc.py"],
            requirements=["add must return a + b"],
            invariants=["Keep the function signature"],
            acceptance_criteria=["addition test passes"],
        ),
        evidence=EvidenceBundle(
            snippets=[
                EvidenceSnippet(
                    path="src/calc.py",
                    start_line=1,
                    end_line=2,
                    content="def add(a, b):\n    return a - b",
                    version=FileVersion(digest="a" * 64, size_bytes=32),
                )
            ]
        ),
    )

    assert set(EditorRequest.model_fields) == {"intent", "evidence"}
    assert "edit_mode" not in EditorRequest.model_fields
    assert "history" not in EditorRequest.model_fields
    assert request.intent.objective == "Fix subtraction typo"
