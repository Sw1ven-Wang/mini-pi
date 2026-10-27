"""V6 ToolDispatcher result observer tests."""

import asyncio
import logging
from pathlib import Path

import pytest

from ai.messages import ToolCall
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.read import ReadTool


class _RecordingObserver:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def on_tool_result(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _FailingObserver:
    async def on_tool_result(self, **kwargs) -> None:
        raise RuntimeError("observer broken")


def _call() -> ToolCall:
    return ToolCall(
        id="call_read",
        name="read",
        arguments={"path": "app.py"},
    )


def _run(coro):
    return asyncio.run(coro)


def _dispatcher(
    root: Path,
    *,
    observer=None,
) -> ToolDispatcher:
    return ToolDispatcher(
        ToolRegistry([ReadTool(Workspace(root))]),
        CodingPermissionEngine(),
        result_observer=observer,
    )


def test_dispatcher_calls_result_observer(tmp_path: Path):
    (tmp_path / "app.py").write_text("hello", encoding="utf-8")
    observer = _RecordingObserver()

    result = _run(
        _dispatcher(
            tmp_path,
            observer=observer,
        ).dispatch(_call())
    )

    assert result.is_error is False
    assert len(observer.calls) == 1
    assert observer.calls[0]["tool_name"] == "read"
    assert observer.calls[0]["arguments"] == {"path": "app.py"}
    assert observer.calls[0]["result"].content == "hello"


def test_dispatcher_without_observer_regression(tmp_path: Path):
    (tmp_path / "app.py").write_text("hello", encoding="utf-8")

    result = _run(_dispatcher(tmp_path).dispatch(_call()))

    assert result.is_error is False
    assert result.content == "hello"


def test_observer_failure_does_not_rewrite_tool_outcome(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    (tmp_path / "app.py").write_text("hello", encoding="utf-8")
    caplog.set_level(logging.ERROR)

    result = _run(
        _dispatcher(
            tmp_path,
            observer=_FailingObserver(),
        ).dispatch(_call())
    )

    assert result.is_error is False
    assert result.content == "hello"
    assert "tool result observer" in caplog.text
