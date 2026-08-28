"""Tests for Workspace path containment."""

from pathlib import Path

import pytest

from coding_agent.errors import (
    PathOutsideWorkspaceError,
)
from coding_agent.filesystem.workspace import Workspace


def test_workspace_root_is_resolved(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    assert workspace.root == tmp_path.resolve()


def test_resolve_file_inside_workspace(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    result = workspace.resolve(
        "hello.txt"
    )

    assert result == (
        tmp_path / "hello.txt"
    ).resolve()


def test_resolve_nested_file_inside_workspace(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    result = workspace.resolve(
        "src/main.py"
    )

    assert result == (
        tmp_path / "src" / "main.py"
    ).resolve()


def test_normalized_path_can_stay_inside_workspace(
    tmp_path: Path,
):
    """包含 .. 不代表一定危险。

    src/../main.py 最终仍然处于 workspace 内，
    因此允许。
    """

    workspace = Workspace(tmp_path)

    result = workspace.resolve(
        "src/../main.py"
    )

    assert result == (
        tmp_path / "main.py"
    ).resolve()


def test_reject_parent_escape(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    with pytest.raises(
        PathOutsideWorkspaceError
    ):
        workspace.resolve(
            "../secret.txt"
        )


def test_reject_deep_parent_escape(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    with pytest.raises(
        PathOutsideWorkspaceError
    ):
        workspace.resolve(
            "../../secret.txt"
        )


def test_reject_absolute_path(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    outside = (
        tmp_path.parent / "secret.txt"
    ).resolve()

    with pytest.raises(ValueError):
        workspace.resolve(
            str(outside)
        )


def test_reject_empty_path(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    with pytest.raises(ValueError):
        workspace.resolve("")


def test_relative_returns_posix_path(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    file_path = (
        tmp_path / "src" / "main.py"
    )

    result = workspace.relative(
        file_path
    )

    assert result == "src/main.py"


def test_relative_rejects_outside_path(
    tmp_path: Path,
):
    workspace = Workspace(tmp_path)

    outside = (
        tmp_path.parent / "secret.txt"
    )

    with pytest.raises(
        PathOutsideWorkspaceError
    ):
        workspace.relative(outside)