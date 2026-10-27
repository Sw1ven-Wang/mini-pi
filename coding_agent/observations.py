"""Observed file state + ObservationStore + tool result observer.

ObservationStore 是 Host last-known state 的缓存，不是磁盘真相。
任何 mutation 最终仍必须经过 disk final recheck。
"""

from __future__ import annotations

import logging
import posixpath
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

from agent.tools import AgentToolResult
from coding_agent.errors import (
    FileNotObservedError,
    InvalidArgumentError,
)
from coding_agent.filesystem.versions import (
    FileVersion,
    same_version,
)

logger = logging.getLogger(__name__)


class ObservationSource(str, Enum):
    READ = "read"
    CREATE = "create"
    EDIT = "edit"
    PATCH = "patch"
    DELETE = "delete"
    MOVE = "move"
    ROLLBACK = "rollback"
    FORMATTER = "formatter"


class ObservedFileState(BaseModel):
    path: str

    exists: bool

    version: FileVersion | None = None

    source: ObservationSource

    observed_at: datetime

    turn_index: int | None = None

    @field_validator("path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        return normalize_observation_path(value)

    @model_validator(mode="after")
    def _validate_file_state(self) -> "ObservedFileState":
        if self.exists and self.version is None:
            raise ValueError(
                "exists=True 时 version 不能为 None"
            )

        if not self.exists and self.version is not None:
            raise ValueError(
                "exists=False 时 version 必须为 None"
            )

        return self


class ObservationStore:
    """Host 保存的 last-known file state。

    path 统一使用 workspace-relative POSIX path。

    重要：Observation 只是缓存，不是磁盘真相。
    磁盘被外部修改后，store 仍然保留旧 observation，
    mutation 时由 disk final recheck 负责拒绝 stale。
    """

    def __init__(self) -> None:
        self._files: dict[str, ObservedFileState] = {}

    def get(
        self,
        path: str,
    ) -> ObservedFileState | None:
        return self._files.get(normalize_observation_path(path))

    def update(
        self,
        observation: ObservedFileState,
    ) -> None:
        self._files[observation.path] = observation

    def remove(
        self,
        path: str,
    ) -> None:
        self._files.pop(normalize_observation_path(path), None)

    def all(self) -> list[ObservedFileState]:
        return [
            self._files[path]
            for path in sorted(self._files)
        ]

    def replace_all(
        self,
        observations: list[ObservedFileState],
    ) -> None:
        """用持久化快照恢复 last-known observations。"""

        self._files = {
            observation.path: observation
            for observation in observations
        }


def normalize_observation_path(path: str) -> str:
    """返回 workspace-relative POSIX path。

    ObservationStore 没有 Workspace 实例，因此这里只做纯词法边界检查；
    真正访问磁盘时仍必须经过 ``Workspace.resolve``。
    """

    if not isinstance(path, str) or not path:
        raise ValueError("observation path 不能为空")

    if "\x00" in path:
        raise ValueError("observation path 不能包含 NUL 字符")

    windows_path = PureWindowsPath(path)

    if windows_path.is_absolute() or windows_path.drive:
        raise ValueError(
            "observation path 必须是 workspace 相对路径"
        )

    normalized = posixpath.normpath(path.replace("\\", "/"))

    if (
        normalized in {"", ".", ".."}
        or PurePosixPath(normalized).is_absolute()
        or normalized.startswith("../")
    ):
        raise ValueError(
            "observation path 必须位于 workspace 内"
        )

    return normalized


class VersionResolver:
    """从 ObservationStore 解析文件版本，让模型无需复制 hash。

    没有 observation → FileNotObservedError。
    Observation 只是缓存，最终仍由 mutation 的 disk recheck 兜底。
    """

    def __init__(
        self,
        observation_store: ObservationStore,
    ) -> None:
        self.store = observation_store

    def require_version(self, path: str) -> FileVersion:
        observation = self.store.get(path)

        if (
            observation is None
            or not observation.exists
            or observation.version is None
        ):
            raise FileNotObservedError(path=path)

        return observation.version

    def resolve_version(
        self,
        path: str,
        explicit_version: Any = None,
    ) -> FileVersion:
        """返回 Host observation，并校验可选的旧式显式版本。

        只要启用了 VersionResolver，就始终要求 prior observation；
        显式 hash 仅用于向后兼容，不能绕过 read-before-mutation。
        """

        observed = self.require_version(path)

        if explicit_version is None:
            return observed

        explicit = FileVersion.model_validate(explicit_version)

        if not same_version(observed, explicit):
            raise InvalidArgumentError(
                "显式文件版本与 Host observation 不一致，"
                "请重新 read 后重试"
            )

        return observed


def resolve_tool_version(
    *,
    path: str,
    explicit_version: Any,
    resolver: VersionResolver | None,
    argument_name: str,
    legacy_optional: bool = False,
) -> FileVersion | None:
    """为文件 mutation 解析 expected version。

    V6 runtime 注入 resolver 后严格执行 prior observation；
    resolver=None 时保留 V1–V5 直接调用 Tool 的兼容行为。
    """

    if resolver is not None:
        return resolver.resolve_version(path, explicit_version)

    if explicit_version is not None:
        return FileVersion.model_validate(explicit_version)

    if legacy_optional:
        return None

    raise InvalidArgumentError(f"缺少 {argument_name}")


class CodingObservationObserver:
    """根据 tool_name + AgentToolResult.details 更新 ObservationStore。"""

    def __init__(
        self,
        store: ObservationStore,
    ) -> None:
        self.store = store

    async def on_tool_result(
        self,
        *,
        tool_name: str,
        arguments: dict,
        result: AgentToolResult,
    ) -> None:
        details = result.details

        if tool_name == "read":
            self._observe_read(details)

        elif tool_name in {"write", "edit"}:
            self._observe_mutation(details, source=_source(tool_name))

        elif tool_name in {"patch", "formatter"}:
            self._observe_mutations(
                details,
                source=(
                    ObservationSource.PATCH
                    if tool_name == "patch"
                    else ObservationSource.FORMATTER
                ),
            )

        elif tool_name == "delete":
            self._observe_delete(arguments, details)

        elif tool_name == "move":
            self._observe_move(arguments, details)

        # 其它工具（glob/search/powershell）不产生文件观察。

    # ------------------------------------------------------------------
    # 各工具观察逻辑
    # ------------------------------------------------------------------

    def _observe_read(self, details: dict) -> None:
        snapshot = _as_mapping(details.get("snapshot"))

        if not snapshot:
            return

        path = snapshot.get("path")
        version = snapshot.get("version")

        if path is None:
            return

        self.store.update(
            ObservedFileState(
                path=path,
                exists=True,
                version=(
                    FileVersion.model_validate(version)
                    if version is not None
                    else None
                ),
                source=ObservationSource.READ,
                observed_at=_now(),
            )
        )

    def _observe_mutation(
        self,
        details: dict,
        *,
        source: ObservationSource,
    ) -> None:
        mutation = _as_mapping(details.get("mutation"))

        if not mutation:
            return

        path = mutation.get("path")
        new_version = mutation.get("new_version")

        if path is None:
            return

        self.store.update(
            ObservedFileState(
                path=path,
                exists=True,
                version=(
                    FileVersion.model_validate(new_version)
                    if new_version is not None
                    else None
                ),
                source=source,
                observed_at=_now(),
            )
        )

    def _observe_mutations(
        self,
        details: dict,
        *,
        source: ObservationSource,
    ) -> None:
        mutations = details.get("mutations", [])

        for raw_mutation in mutations:
            mutation = _as_mapping(raw_mutation)

            if not mutation:
                continue

            path = mutation.get("path")
            new_version = mutation.get("new_version")

            if path is None:
                continue

            self.store.update(
                ObservedFileState(
                    path=path,
                    exists=new_version is not None,
                    version=_version_or_none(new_version),
                    source=source,
                    observed_at=_now(),
                )
            )

    def _observe_delete(
        self,
        arguments: dict,
        details: dict,
    ) -> None:
        path = arguments.get("path")

        if path is None:
            return

        self.store.update(
            ObservedFileState(
                path=path,
                exists=False,
                version=None,
                source=ObservationSource.DELETE,
                observed_at=_now(),
            )
        )

    def _observe_move(
        self,
        arguments: dict,
        details: dict,
    ) -> None:
        source = arguments.get("source")
        destination = arguments.get("destination")

        mutation = _as_mapping(details.get("mutation"))

        if source is not None:
            self.store.update(
                ObservedFileState(
                    path=source,
                    exists=False,
                    version=None,
                    source=ObservationSource.MOVE,
                    observed_at=_now(),
                )
            )

        if destination is not None:
            moved_version = None

            if mutation:
                moved_version = mutation.get("new_version")

            self.store.update(
                ObservedFileState(
                    path=destination,
                    exists=True,
                    version=(
                        FileVersion.model_validate(moved_version)
                        if moved_version is not None
                        else None
                    ),
                    source=ObservationSource.MOVE,
                    observed_at=_now(),
                )
            )


def _source(tool_name: str) -> ObservationSource:
    if tool_name == "write":
        return ObservationSource.CREATE

    return ObservationSource.EDIT


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, BaseModel):
        return value.model_dump()

    if isinstance(value, Mapping):
        return value

    return None


def _version_or_none(value: Any) -> FileVersion | None:
    if value is None:
        return None

    return FileVersion.model_validate(value)
