"""Tests for ToolExecutor."""

import asyncio
from pathlib import Path

from ai.messages import ToolCall
from agent.tool_executor import ToolExecutor
from coding_agent.tools.read import ReadTool


def _run(coro):
    """运行一个 async coroutine。"""
    return asyncio.run(coro)


def test_execute_read_tool_success(tmp_path: Path):
    """正常执行 ReadTool，应返回成功 ToolResultMessage。"""

    file_path = tmp_path / "hello.txt"
    file_path.write_text("hello mini pi", encoding="utf-8")

    read_tool = ReadTool(tmp_path)
    executor = ToolExecutor([read_tool])

    tool_call = ToolCall(
        id="call_1",
        name="read",
        arguments={
            "path": "hello.txt",
        },
    )

    result = _run(executor.execute(tool_call))

    assert result.tool_call_id == "call_1"
    assert result.name == "read"
    assert result.content == "hello mini pi"
    assert result.is_error is False


def test_execute_unknown_tool():
    """工具不存在时，不应抛异常，而应返回错误 ToolResultMessage。"""

    executor = ToolExecutor([])

    tool_call = ToolCall(
        id="call_2",
        name="unknown_tool",
        arguments={},
    )

    result = _run(executor.execute(tool_call))

    assert result.tool_call_id == "call_2"
    assert result.name == "unknown_tool"
    assert result.is_error is True
    assert "unknown_tool" in result.content


def test_execute_tool_exception_becomes_error_result(tmp_path: Path):
    """具体工具抛异常时，ToolExecutor 应转换为错误结果。"""

    read_tool = ReadTool(tmp_path)
    executor = ToolExecutor([read_tool])

    tool_call = ToolCall(
        id="call_3",
        name="read",
        arguments={
            "path": "missing.txt",
        },
    )

    result = _run(executor.execute(tool_call))

    assert result.tool_call_id == "call_3"
    assert result.name == "read"
    assert result.is_error is True
    assert result.content != ""


def test_execute_missing_argument_becomes_error_result(tmp_path: Path):
    """缺少必要参数时，也应由 ToolExecutor 捕获并转为错误结果。"""

    read_tool = ReadTool(tmp_path)
    executor = ToolExecutor([read_tool])

    tool_call = ToolCall(
        id="call_4",
        name="read",
        arguments={},
    )

    result = _run(executor.execute(tool_call))

    assert result.tool_call_id == "call_4"
    assert result.name == "read"
    assert result.is_error is True
    assert "path" in result.content