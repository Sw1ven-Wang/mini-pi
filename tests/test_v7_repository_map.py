"""V7 Stop Rule E: minimal AST repository map."""

from pathlib import Path

from coding_agent.filesystem.workspace import Workspace
from coding_agent.repository_map import RepositoryMap, RepositoryMapBuilder


def test_repository_map_lists_top_level_symbols_and_imports(tmp_path: Path):
    (tmp_path / "app.py").write_text(
        "import os\n"
        "from pkg.tools import helper\n\n"
        "class Service:\n"
        "    def method(self):\n"
        "        return 1\n\n"
        "async def run():\n"
        "    def nested():\n"
        "        return 2\n"
        "    return os.name\n",
        encoding="utf-8",
    )

    result = RepositoryMapBuilder(Workspace(tmp_path)).build()

    assert [item.path for item in result.files] == ["app.py"]
    assert [item.name for item in result.files[0].classes] == ["Service"]
    assert [item.name for item in result.files[0].functions] == ["run"]
    assert result.files[0].imports == ["os", "pkg.tools:helper"]


def test_repository_map_is_navigation_only_and_has_no_source_content(tmp_path: Path):
    (tmp_path / "secret.py").write_text("TOKEN = 'do-not-copy'\n", encoding="utf-8")

    result = RepositoryMapBuilder(Workspace(tmp_path)).build()
    payload = result.model_dump()

    assert "content" not in RepositoryMap.model_fields
    assert "do-not-copy" not in str(payload)
    assert "secret.py" in result.render_summary()


def test_repository_map_reports_syntax_errors_without_dropping_file(tmp_path: Path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = RepositoryMapBuilder(Workspace(tmp_path)).build()

    assert [item.path for item in result.files] == ["broken.py"]
    assert any("Could not parse broken.py" in note for note in result.notes)


def test_repository_map_is_stable_and_bounded(tmp_path: Path):
    for name in ["c.py", "a.py", "b.py"]:
        (tmp_path / name).write_text(f"def {name[0]}():\n    pass\n", encoding="utf-8")

    result = RepositoryMapBuilder(Workspace(tmp_path), max_files=2).build()

    assert [item.path for item in result.files] == ["a.py", "b.py"]
    assert result.truncated is True
    assert any("file limit" in note for note in result.notes)


def test_repository_map_summary_is_bounded(tmp_path: Path):
    (tmp_path / "symbols.py").write_text(
        "def alpha():\n    pass\ndef beta():\n    pass\n",
        encoding="utf-8",
    )
    result = RepositoryMapBuilder(Workspace(tmp_path)).build()

    assert len(result.render_summary(max_chars=20)) <= 20
