import asyncio
from pathlib import Path

import pytest

from coding_agent.errors import (
    FileNotFoundAgentError,
    InvalidArgumentError,
)
from coding_agent.filesystem.workspace import Workspace
from coding_agent.tools.read import ReadTool


def test_read_tool_success(tmp_path: Path):
    file = tmp_path / "hello.txt"
    file.write_text("hello mini pi", encoding="utf-8")

    tool = ReadTool(Workspace(tmp_path))

    result = asyncio.run(
        tool.execute(
            "call_1",
            {"path": "hello.txt"},
        )
    )

    assert result.content == "hello mini pi"
    assert (
        result.details["snapshot"]["path"]
        == "hello.txt"
    )


def test_read_tool_missing_path(tmp_path: Path):
    tool = ReadTool(Workspace(tmp_path))

    with pytest.raises(InvalidArgumentError):
        asyncio.run(
            tool.execute("call_1", {})
        )


def test_read_tool_file_not_found(tmp_path: Path):
    tool = ReadTool(Workspace(tmp_path))

    with pytest.raises(FileNotFoundAgentError):
        asyncio.run(
            tool.execute(
                "call_1",
                {"path": "missing.txt"},
            )
        )


def test_read_tool_utf8(tmp_path: Path):
    file = tmp_path / "中文.txt"
    file.write_text("你好，Mini-Pi", encoding="utf-8")

    tool = ReadTool(Workspace(tmp_path))

    result = asyncio.run(
        tool.execute(
            "call_1",
            {"path": "中文.txt"},
        )
    )

    assert result.content == "你好，Mini-Pi"
