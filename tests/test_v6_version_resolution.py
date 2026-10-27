"""V6 Host-side version resolution and stale safety tests."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.errors import (
    FileNotObservedError,
    InvalidArgumentError,
    StaleVersionError,
)
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import (
    ObservationSource,
    ObservationStore,
    ObservedFileState,
    VersionResolver,
)
from coding_agent.tools.delete import DeleteTool
from coding_agent.tools.edit import EditTool
from coding_agent.tools.move import MoveTool
from coding_agent.tools.patch import PatchTool


def _run(coro):
    return asyncio.run(coro)


def _observe(store: ObservationStore, root: Path, path: str) -> None:
    store.update(
        ObservedFileState(
            path=path,
            exists=True,
            version=version_of_file(root / path),
            source=ObservationSource.READ,
            observed_at=datetime.now(timezone.utc),
        )
    )


def _runtime(root: Path):
    workspace = Workspace(root)
    queue = FileMutationQueue()
    checkpoints = CheckpointStore()
    store = ObservationStore()
    resolver = VersionResolver(store)
    return workspace, queue, checkpoints, store, resolver


def test_version_resolver_known_file(tmp_path: Path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    store = ObservationStore()
    _observe(store, tmp_path, "app.py")

    assert VersionResolver(store).require_version(
        "app.py"
    ) == version_of_file(path)


def test_version_resolver_requires_read():
    with pytest.raises(FileNotObservedError):
        VersionResolver(ObservationStore()).require_version("app.py")


def test_explicit_version_cannot_bypass_host_observation(tmp_path: Path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    store = ObservationStore()
    _observe(store, tmp_path, "app.py")
    explicit = version_of_file(path).model_copy(
        update={"digest": "different"}
    )

    with pytest.raises(InvalidArgumentError):
        VersionResolver(store).resolve_version("app.py", explicit)


def test_edit_uses_observed_version(tmp_path: Path):
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")
    workspace, queue, checkpoints, store, resolver = _runtime(tmp_path)
    _observe(store, tmp_path, "app.py")
    tool = EditTool(
        workspace,
        queue,
        checkpoints,
        version_resolver=resolver,
    )

    _run(
        tool.execute(
            "edit",
            {
                "path": "app.py",
                "edits": [
                    {"old_text": "1", "new_text": "2"}
                ],
            },
        )
    )

    assert path.read_text(encoding="utf-8") == "value = 2\n"


def test_delete_uses_observed_version(tmp_path: Path):
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    workspace, queue, checkpoints, store, resolver = _runtime(tmp_path)
    _observe(store, tmp_path, "app.py")
    tool = DeleteTool(
        workspace,
        queue,
        checkpoints,
        version_resolver=resolver,
    )

    _run(tool.execute("delete", {"path": "app.py"}))

    assert not path.exists()


def test_move_uses_observed_source_version(tmp_path: Path):
    source = tmp_path / "old.py"
    source.write_text("old", encoding="utf-8")
    workspace, queue, checkpoints, store, resolver = _runtime(tmp_path)
    _observe(store, tmp_path, "old.py")
    tool = MoveTool(
        workspace,
        queue,
        checkpoints,
        version_resolver=resolver,
    )

    _run(
        tool.execute(
            "move",
            {"source": "old.py", "destination": "new.py"},
        )
    )

    assert not source.exists()
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "old"


def test_patch_uses_observed_versions(tmp_path: Path):
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("a = 1\n", encoding="utf-8")
    second.write_text("b = 1\n", encoding="utf-8")
    workspace, queue, checkpoints, store, resolver = _runtime(tmp_path)
    _observe(store, tmp_path, "a.py")
    _observe(store, tmp_path, "b.py")
    tool = PatchTool(
        workspace,
        queue,
        checkpoints,
        version_resolver=resolver,
    )

    _run(
        tool.execute(
            "patch",
            {
                "files": [
                    {
                        "path": "a.py",
                        "replacements": [
                            {"old_text": "1", "new_text": "2"}
                        ],
                    },
                    {
                        "path": "b.py",
                        "replacements": [
                            {"old_text": "1", "new_text": "2"}
                        ],
                    },
                ]
            },
        )
    )

    assert first.read_text(encoding="utf-8") == "a = 2\n"
    assert second.read_text(encoding="utf-8") == "b = 2\n"


@pytest.mark.parametrize("kind", ["edit", "delete", "move", "patch"])
def test_mutation_requires_prior_observation(tmp_path: Path, kind: str):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    workspace, queue, checkpoints, _store, resolver = _runtime(tmp_path)

    if kind == "edit":
        tool = EditTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute(
            "call",
            {
                "path": "app.py",
                "edits": [{"old_text": "1", "new_text": "2"}],
            },
        )
    elif kind == "delete":
        tool = DeleteTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute("call", {"path": "app.py"})
    elif kind == "move":
        tool = MoveTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute(
            "call",
            {"source": "app.py", "destination": "new.py"},
        )
    else:
        tool = PatchTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute(
            "call",
            {
                "files": [
                    {
                        "path": "app.py",
                        "replacements": [
                            {"old_text": "1", "new_text": "2"}
                        ],
                    }
                ]
            },
        )

    with pytest.raises(FileNotObservedError):
        _run(coro)

    assert (tmp_path / "app.py").read_text(
        encoding="utf-8"
    ) == "value = 1\n"


@pytest.mark.parametrize("kind", ["edit", "delete", "move", "patch"])
def test_observed_version_stale_rejected_by_disk(tmp_path: Path, kind: str):
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")
    workspace, queue, checkpoints, store, resolver = _runtime(tmp_path)
    _observe(store, tmp_path, "app.py")

    path.write_text("external = 99\n", encoding="utf-8")

    if kind == "edit":
        tool = EditTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute(
            "call",
            {
                "path": "app.py",
                "edits": [{"old_text": "1", "new_text": "2"}],
            },
        )
    elif kind == "delete":
        tool = DeleteTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute("call", {"path": "app.py"})
    elif kind == "move":
        tool = MoveTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute(
            "call",
            {"source": "app.py", "destination": "new.py"},
        )
    else:
        tool = PatchTool(
            workspace, queue, checkpoints, version_resolver=resolver
        )
        coro = tool.execute(
            "call",
            {
                "files": [
                    {
                        "path": "app.py",
                        "replacements": [
                            {"old_text": "1", "new_text": "2"}
                        ],
                    }
                ]
            },
        )

    with pytest.raises(StaleVersionError):
        _run(coro)

    assert path.read_text(encoding="utf-8") == "external = 99\n"
    assert not (tmp_path / "new.py").exists()


def test_llm_tool_schemas_do_not_expose_version_hashes():
    assert "expected_version" not in EditTool.parameters["properties"]
    assert "expected_source_version" not in MoveTool.parameters["properties"]
    patch_file_properties = PatchTool.parameters["properties"]["files"][
        "items"
    ]["properties"]
    assert "expected_version" not in patch_file_properties
