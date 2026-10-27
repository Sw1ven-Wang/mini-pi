"""Tests for literal text search runtime."""

import asyncio
from pathlib import Path

from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.text_search import TextSearchRuntime


def _run(coro):
    return asyncio.run(coro)


def _make_tree(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "def old_add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.py").write_text(
        "from src.a import old_add\n"
        "old_add(1, 2)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "hello world\n",
        encoding="utf-8",
    )


def _search(tmp_path: Path, query: str, **kwargs):
    runtime = TextSearchRuntime(Workspace(tmp_path))
    return _run(runtime.search_text(query, **kwargs))


def test_search_finds_literal(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(tmp_path, "old_add")

    paths = {m.path for m in result.matches}
    assert "src/a.py" in paths
    assert "src/b.py" in paths


def test_search_returns_line_number(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(tmp_path, "old_add")

    # b.py 中 old_add 出现在两行。
    b_matches = [m for m in result.matches if m.path == "src/b.py"]

    assert {m.line for m in b_matches} == {1, 2}


def test_search_multiple_files(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(tmp_path, "old_add")

    paths = {m.path for m in result.matches}
    assert paths == {"src/a.py", "src/b.py"}


def test_search_glob_filter(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(
        tmp_path,
        "old_add",
        glob="src/*.py",
    )

    assert {m.path for m in result.matches} == {
        "src/a.py",
        "src/b.py",
    }

    # 过滤到单个文件。
    result = _search(
        tmp_path,
        "old_add",
        glob="src/a.py",
    )

    assert {m.path for m in result.matches} == {"src/a.py"}


def test_search_respects_max_results(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(
        tmp_path,
        "old_add",
        max_results=1,
    )

    assert len(result.matches) == 1
    assert result.truncated is True


def test_search_binary_file_is_skipped(tmp_path: Path):
    _make_tree(tmp_path)

    (tmp_path / "blob.bin").write_bytes(
        b"old_add\x00\x00\x01\x02"
    )

    result = _search(tmp_path, "old_add")

    assert "blob.bin" not in {
        m.path for m in result.matches
    }


def test_search_utf8_bom(tmp_path: Path):
    (tmp_path / "bom.py").write_bytes(
        b"\xef\xbb\xbf"
        b"old_add(a, b)\n"
    )

    result = _search(tmp_path, "old_add")

    assert "bom.py" in {m.path for m in result.matches}


def test_search_workspace_relative_paths(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(tmp_path, "old_add")

    for match in result.matches:
        assert "\\" not in match.path
        assert not match.path.startswith(str(tmp_path))


def test_search_no_match(tmp_path: Path):
    _make_tree(tmp_path)

    result = _search(tmp_path, "does_not_exist")

    assert result.matches == []
    assert result.truncated is False
