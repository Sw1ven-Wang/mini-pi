"""Tests for Glob runtime."""

import asyncio
from pathlib import Path

import pytest

from coding_agent.errors import InvalidArgumentError
from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.glob import GlobRuntime


def _run(coro):
    return asyncio.run(coro)


def _make_tree(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "app.py").write_text("a = 1")
    (tmp_path / "src" / "utils.py").write_text("b = 2")
    (tmp_path / "tests" / "test_app.py").write_text("def test(): pass")
    (tmp_path / "README.md").write_text("hi")


def _glob(tmp_path: Path, pattern: str, **kwargs):
    runtime = GlobRuntime(Workspace(tmp_path))
    return _run(runtime.glob(pattern, **kwargs))


def test_glob_finds_python_files(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "**/*.py")

    paths = {m.path for m in result.matches}
    assert paths == {
        "src/app.py",
        "src/utils.py",
        "tests/test_app.py",
    }


def test_glob_returns_workspace_relative_paths(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "**/*.py")

    for match in result.matches:
        assert not match.path.startswith(str(tmp_path))
        assert "\\" not in match.path


def test_glob_results_are_sorted(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "**/*.py")

    paths = [m.path for m in result.matches]
    assert paths == sorted(paths)


def test_glob_rejects_workspace_escape(tmp_path: Path):
    for bad in ("../../**", "C:\\**", "/home/**"):
        with pytest.raises(InvalidArgumentError):
            _glob(tmp_path, bad)


def test_glob_respects_max_results(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "**/*.py", max_results=2)

    assert len(result.matches) == 2
    assert result.truncated is True


def test_glob_skips_default_ignored_dirs(tmp_path: Path):
    _make_tree(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "site.py").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("x")

    result = _glob(tmp_path, "**/*")

    paths = {m.path for m in result.matches}
    assert ".venv/site.py" not in paths
    assert "__pycache__/x.pyc" not in paths
    assert "src/app.py" in paths


def test_glob_empty_result(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "**/*.rs")

    assert result.matches == []
    assert result.truncated is False


def test_glob_matches_directories(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "**")

    dirs = {
        m.path
        for m in result.matches
        if not m.is_file
    }

    assert "src" in dirs
    assert "tests" in dirs
    assert all(m.is_file for m in result.matches if m.path.endswith(".py"))


# ---------------------------------------------------------------------------
# V3 审计 0.3：glob→regex 语义充分测试
# ---------------------------------------------------------------------------


def test_glob_root_level_python(tmp_path: Path):
    """**/*.py 必须匹配 workspace 根目录 Python 文件（零层目录）。"""
    (tmp_path / "root.py").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("x")

    result = _glob(tmp_path, "**/*.py")

    assert {m.path for m in result.matches} == {
        "root.py",
        "sub/deep.py",
    }


def test_glob_src_recursive(tmp_path: Path):
    _make_tree(tmp_path)
    (tmp_path / "src" / "nested").mkdir()
    (tmp_path / "src" / "nested" / "deep.py").write_text("x")

    result = _glob(tmp_path, "src/**/*.py")

    assert {m.path for m in result.matches} == {
        "src/app.py",
        "src/utils.py",
        "src/nested/deep.py",
    }


def test_glob_single_star_not_recursive(tmp_path: Path):
    _make_tree(tmp_path)
    (tmp_path / "top.py").write_text("x")

    result = _glob(tmp_path, "*.py")

    # *.py 只匹配根目录，不递归。
    assert {m.path for m in result.matches} == {"top.py"}


def test_glob_char_class(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "src" / "b.py").write_text("x")
    (tmp_path / "src" / "c.py").write_text("x")

    result = _glob(tmp_path, "src/[ab].py")

    assert {m.path for m in result.matches} == {
        "src/a.py",
        "src/b.py",
    }


def test_glob_hidden_files(tmp_path: Path):
    (tmp_path / ".env").write_text("x")
    (tmp_path / "normal.txt").write_text("x")

    result = _glob(tmp_path, "**/*")

    paths = {m.path for m in result.matches}
    assert ".env" in paths
    assert "normal.txt" in paths


def test_glob_windows_separator(tmp_path: Path):
    _make_tree(tmp_path)

    result = _glob(tmp_path, "src\\**\\*.py")

    assert {m.path for m in result.matches} == {
        "src/app.py",
        "src/utils.py",
    }
