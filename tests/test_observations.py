"""V6 observed file state tests."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agent.tools import AgentToolResult
from coding_agent.filesystem.versions import FileVersion
from coding_agent.observations import (
    CodingObservationObserver,
    ObservationSource,
    ObservationStore,
    ObservedFileState,
)


def _version(digest: str = "a") -> FileVersion:
    return FileVersion(digest=digest, size_bytes=1)


def _run(coro):
    return asyncio.run(coro)


def _observed(
    path: str,
    *,
    digest: str = "a",
    source: ObservationSource = ObservationSource.READ,
    observed_at: datetime | None = None,
) -> ObservedFileState:
    return ObservedFileState(
        path=path,
        exists=True,
        version=_version(digest),
        source=source,
        observed_at=observed_at or datetime.now(timezone.utc),
    )


def test_observation_store_read_update():
    store = ObservationStore()
    observation = _observed("src/app.py")

    store.update(observation)

    assert store.get("src/app.py") == observation
    assert store.all() == [observation]


def test_observation_store_delete_marks_missing():
    store = ObservationStore()
    store.update(_observed("src/app.py"))

    missing = ObservedFileState(
        path="src/app.py",
        exists=False,
        version=None,
        source=ObservationSource.DELETE,
        observed_at=datetime.now(timezone.utc),
    )
    store.update(missing)

    assert store.get("src/app.py") == missing


def test_observation_store_move_updates_two_paths():
    store = ObservationStore()
    now = datetime.now(timezone.utc)
    source = ObservedFileState(
        path="src/old.py",
        exists=False,
        source=ObservationSource.MOVE,
        observed_at=now,
    )
    destination = _observed(
        "src/new.py",
        source=ObservationSource.MOVE,
        observed_at=now,
    )

    store.update(source)
    store.update(destination)

    assert store.get("src/old.py") == source
    assert store.get("src/new.py") == destination


def test_observation_store_latest_update_wins():
    store = ObservationStore()
    now = datetime.now(timezone.utc)
    first = _observed("app.py", digest="1", observed_at=now)
    latest = _observed(
        "app.py",
        digest="2",
        observed_at=now + timedelta(seconds=1),
    )

    store.update(first)
    store.update(latest)

    assert store.get("app.py") == latest


def test_observation_path_is_workspace_relative_posix():
    store = ObservationStore()
    observation = _observed(r"src\pkg\app.py")
    store.update(observation)

    assert observation.path == "src/pkg/app.py"
    assert store.get(r"src\pkg\app.py") == observation

    with pytest.raises(ValidationError):
        _observed("../outside.py")

    with pytest.raises(ValidationError):
        _observed(r"C:\outside.py")


def test_observed_missing_has_no_version():
    with pytest.raises(ValidationError):
        ObservedFileState(
            path="gone.py",
            exists=False,
            version=_version(),
            source=ObservationSource.DELETE,
            observed_at=datetime.now(timezone.utc),
        )


def test_observed_existing_requires_version():
    with pytest.raises(ValidationError):
        ObservedFileState(
            path="app.py",
            exists=True,
            version=None,
            source=ObservationSource.READ,
            observed_at=datetime.now(timezone.utc),
        )


def test_observation_is_not_disk_truth(tmp_path):
    file_path = tmp_path / "app.py"
    file_path.write_text("old", encoding="utf-8")
    store = ObservationStore()
    old = _observed("app.py", digest="old")
    store.update(old)

    file_path.write_text("externally changed", encoding="utf-8")

    assert store.get("app.py") == old


def _result(**details) -> AgentToolResult:
    return AgentToolResult(details=details)


def test_read_result_updates_observation():
    store = ObservationStore()
    observer = CodingObservationObserver(store)

    _run(
        observer.on_tool_result(
            tool_name="read",
            arguments={"path": "app.py"},
            result=_result(
                snapshot={
                    "path": "app.py",
                    "version": _version("read").model_dump(),
                }
            ),
        )
    )

    observation = store.get("app.py")
    assert observation is not None
    assert observation.source == ObservationSource.READ
    assert observation.version == _version("read")


@pytest.mark.parametrize(
    ("tool_name", "source"),
    [
        ("write", ObservationSource.CREATE),
        ("edit", ObservationSource.EDIT),
    ],
)
def test_single_file_mutation_updates_new_version(
    tool_name: str,
    source: ObservationSource,
):
    store = ObservationStore()
    observer = CodingObservationObserver(store)

    _run(
        observer.on_tool_result(
            tool_name=tool_name,
            arguments={"path": "app.py"},
            result=_result(
                mutation={
                    "path": "app.py",
                    "new_version": _version(tool_name).model_dump(),
                }
            ),
        )
    )

    observation = store.get("app.py")
    assert observation is not None
    assert observation.source == source
    assert observation.version == _version(tool_name)


def test_delete_result_marks_file_missing():
    store = ObservationStore()
    observer = CodingObservationObserver(store)

    _run(
        observer.on_tool_result(
            tool_name="delete",
            arguments={"path": "app.py"},
            result=_result(mutation={"path": "app.py"}),
        )
    )

    observation = store.get("app.py")
    assert observation is not None
    assert observation.exists is False
    assert observation.version is None


def test_move_result_updates_source_and_destination():
    store = ObservationStore()
    observer = CodingObservationObserver(store)

    _run(
        observer.on_tool_result(
            tool_name="move",
            arguments={"source": "old.py", "destination": "new.py"},
            result=_result(
                mutation={
                    "path": "new.py",
                    "new_version": _version("moved").model_dump(),
                }
            ),
        )
    )

    source = store.get("old.py")
    destination = store.get("new.py")
    assert source is not None and source.exists is False
    assert destination is not None and destination.exists is True
    assert destination.version == _version("moved")


@pytest.mark.parametrize(
    ("tool_name", "source"),
    [
        ("patch", ObservationSource.PATCH),
        ("formatter", ObservationSource.FORMATTER),
    ],
)
def test_multi_file_result_updates_all_files(
    tool_name: str,
    source: ObservationSource,
):
    store = ObservationStore()
    observer = CodingObservationObserver(store)

    _run(
        observer.on_tool_result(
            tool_name=tool_name,
            arguments={},
            result=_result(
                mutations=[
                    {
                        "path": "a.py",
                        "new_version": _version("a").model_dump(),
                    },
                    {
                        "path": "b.py",
                        "new_version": _version("b").model_dump(),
                    },
                ]
            ),
        )
    )

    assert store.get("a.py").source == source  # type: ignore[union-attr]
    assert store.get("b.py").source == source  # type: ignore[union-attr]
