"""Tests for ToolRegistry."""

from pathlib import Path

import pytest

from agent.tool_registry import ToolRegistry
from coding_agent.filesystem.workspace import Workspace
from coding_agent.tools.read import ReadTool
from coding_agent.tools.write import WriteTool


def test_registry_registers_tools(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    read_tool = ReadTool(workspace)
    write_tool = WriteTool(workspace)

    registry = ToolRegistry(
        [
            read_tool,
            write_tool,
        ]
    )

    assert len(registry) == 2

    assert registry.get("read") is read_tool
    assert registry.get("write") is write_tool


def test_registry_unknown_tool_returns_none(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    registry = ToolRegistry(
        [
            ReadTool(workspace),
        ]
    )

    assert registry.get("does_not_exist") is None


def test_registry_rejects_duplicate_name(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    read_1 = ReadTool(workspace)
    read_2 = ReadTool(workspace)

    with pytest.raises(
        ValueError,
        match="工具名称重复",
    ):
        ToolRegistry(
            [
                read_1,
                read_2,
            ]
        )


def test_registry_exports_tool_definitions(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    registry = ToolRegistry(
        [
            ReadTool(workspace),
            WriteTool(workspace),
        ]
    )

    definitions = registry.definitions

    assert len(definitions) == 2

    names = {
        definition.name
        for definition in definitions
    }

    assert names == {
        "read",
        "write",
    }


def test_registry_contains_tool(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    registry = ToolRegistry(
        [
            ReadTool(workspace),
        ]
    )

    assert "read" in registry
    assert "write" not in registry