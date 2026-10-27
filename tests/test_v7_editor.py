"""V7 Stop Rule G: isolated single-model Editor."""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from ai.context import Context
from ai.events import AIEvent, StreamEnd, StreamStart
from ai.messages import AssistantMessage
from ai.models import Model
from ai.tools import ToolDefinition
from coding_agent.agents.editor import Editor
from coding_agent.agents.models import (
    ChangeIntent,
    EditorRequest,
    EvidenceBundle,
    EvidenceSnippet,
)
from coding_agent.changes.workspace_ir import EditFileOp
from coding_agent.errors import EditingError
from coding_agent.filesystem.versions import FileVersion


class _FakeProvider:
    def __init__(self, content: str, *, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.calls: list[dict] = []

    async def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        self.calls.append({"model": model, "context": context, "tools": tools})
        yield StreamStart()
        yield StreamEnd(
            message=AssistantMessage(
                content=self.content,
                tool_calls=self.tool_calls,
            )
        )


def _run(coro):
    return asyncio.run(coro)


def _request() -> EditorRequest:
    return EditorRequest(
        intent=ChangeIntent(
            objective="Fix add",
            target_files=["src/calc.py"],
            requirements=["Return a + b"],
            invariants=["Keep signature"],
            acceptance_criteria=["Test passes"],
        ),
        evidence=EvidenceBundle(
            snippets=[
                EvidenceSnippet(
                    path="src/calc.py",
                    start_line=1,
                    end_line=2,
                    content="def add(a, b):\n    return a - b",
                    version=FileVersion(
                        digest="a" * 64,
                        size_bytes=31,
                        mtime_ns=10,
                    ),
                )
            ]
        ),
    )


def _edit_payload(*, path="src/calc.py", old_text="return a - b") -> str:
    return json.dumps(
        {
            "operations": [
                {
                    "kind": "edit_file",
                    "path": path,
                    "replacements": [
                        {"old_text": old_text, "new_text": "return a + b"}
                    ],
                }
            ]
        }
    )


def test_editor_returns_workspace_change_set_and_host_injects_version():
    result = _run(
        Editor(_FakeProvider(_edit_payload()), Model(id="one", provider="fake"))
        .create_change_set(request=_request())
    )

    assert isinstance(result.operations[0], EditFileOp)
    assert result.operations[0].expected_version.digest == "a" * 64


def test_editor_uses_shared_model_and_no_tools():
    provider = _FakeProvider(_edit_payload())
    model = Model(id="shared-model", provider="fake")
    _run(Editor(provider, model).create_change_set(request=_request()))

    assert provider.calls[0]["model"] is model
    assert provider.calls[0]["tools"] == []


def test_editor_context_is_isolated_to_request_and_diagnostics():
    provider = _FakeProvider(_edit_payload())
    _run(
        Editor(provider, Model(id="one", provider="fake")).create_change_set(
            request=_request()
        )
    )

    payload = json.loads(provider.calls[0]["context"].messages[0].content)
    assert set(payload) == {"request", "diagnostics"}
    assert "history" not in str(payload)
    assert payload["request"]["evidence"]["snippets"][0]["version"]["digest"]


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"operations":[]}',
        _edit_payload(path="src/unrelated.py"),
        _edit_payload(old_text="not present in evidence"),
    ],
)
def test_editor_rejects_invalid_or_unsafe_output(content: str):
    with pytest.raises(EditingError):
        _run(
            Editor(_FakeProvider(content), Model(id="one", provider="fake"))
            .create_change_set(request=_request())
        )


def test_editor_allows_create_only_when_declared_by_intent():
    request = _request().model_copy(
        update={
            "intent": _request().intent.model_copy(
                update={"target_files": ["src/calc.py", "src/new.py"]}
            )
        }
    )
    content = json.dumps(
        {
            "operations": [
                {"kind": "create_file", "path": "src/new.py", "content": "x = 1\n"}
            ]
        }
    )

    result = _run(
        Editor(_FakeProvider(content), Model(id="one", provider="fake"))
        .create_change_set(request=request)
    )

    assert result.operations[0].kind == "create_file"
