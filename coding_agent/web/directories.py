"""Canonical, allow-root constrained directory browsing."""

from __future__ import annotations

from pathlib import Path, PurePath

from coding_agent.web.config import AllowedRoot
from coding_agent.web.models import DirectoryEntry, DirectoryListing


class DirectoryBrowser:
    def __init__(self, roots: list[AllowedRoot]) -> None:
        self._roots = {root.id: root for root in roots}
        if not self._roots:
            raise ValueError("at least one allowed root is required")
        if len(self._roots) != len(roots):
            raise ValueError("allowed root ids must be unique")

    @property
    def roots(self) -> list[AllowedRoot]:
        return list(self._roots.values())

    def resolve(self, root_id: str, relative_path: str) -> Path:
        root = self._roots.get(root_id)
        if root is None:
            raise KeyError("unknown allowed root")
        normalized = relative_path.replace("\\", "/").strip("/")
        pure = PurePath(normalized)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            if normalized:
                raise ValueError("workspace path must be a normalized relative path")
        candidate = (root.path / normalized).resolve(strict=True)
        if not candidate.is_relative_to(root.path) or not candidate.is_dir():
            raise PermissionError("directory escapes its allowed root")
        return candidate

    def browse(self, root_id: str, relative_path: str = "") -> DirectoryListing:
        directory = self.resolve(root_id, relative_path)
        root = self._roots[root_id]
        current = directory.relative_to(root.path).as_posix()
        current = "" if current == "." else current
        entries: list[DirectoryEntry] = []
        for item in directory.iterdir():
            try:
                resolved = item.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not resolved.is_dir() or not resolved.is_relative_to(root.path):
                continue
            relative = resolved.relative_to(root.path).as_posix()
            entries.append(DirectoryEntry(name=item.name, relative_path=relative))
        entries.sort(key=lambda item: item.name.casefold())
        parent = None
        if current:
            parent_value = Path(current).parent.as_posix()
            parent = "" if parent_value == "." else parent_value
        return DirectoryListing(
            root_id=root_id,
            relative_path=current,
            parent_path=parent,
            directories=entries,
        )
