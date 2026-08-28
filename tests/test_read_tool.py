import asyncio
from pathlib import Path

import pytest

from coding_agent.tools.read import ReadTool


def test_read_tool_success(tmp_path: Path):
    file = tmp_path / "hello.txt"
    file.write_text("hello mini pi", encoding="utf-8")

    tool = ReadTool(tmp_path)

    result = asyncio.run(
        tool.execute(
            "call_1",
            {"path": "hello.txt"},
        )
    )

    assert result.content == "hello mini pi"
    assert result.details["path"] == str(file.resolve())
    assert result.details["bytes"] == len("hello mini pi".encode("utf-8"))


def test_read_tool_missing_path(tmp_path: Path):
    tool = ReadTool(tmp_path)

    with pytest.raises(ValueError):
        asyncio.run(
            tool.execute(
                "call_1",
                {},
            )
        )


def test_read_tool_file_not_found(tmp_path: Path):
    tool = ReadTool(tmp_path)

    with pytest.raises(FileNotFoundError):
        asyncio.run(
            tool.execute(
                "call_1",
                {"path": "missing.txt"},
            )
        )


def test_read_tool_utf8(tmp_path: Path):
    file = tmp_path / "中文.txt"
    file.write_text("你好，Mini-Pi", encoding="utf-8")

    tool = ReadTool(tmp_path)

    result = asyncio.run(
        tool.execute(
            "call_1",
            {"path": "中文.txt"},
        )
    )

    assert result.content == "你好，Mini-Pi"