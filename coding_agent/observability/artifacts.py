"""Atomic content-addressed artifact storage."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from coding_agent.observability.redaction import redact_text


_HASH = re.compile(r"^[0-9a-f]{64}$")


class ArtifactRef(BaseModel):
    artifact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    relative_path: str = Field(min_length=1)


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)

    def put_text(self, content: str, *, kind: str) -> ArtifactRef:
        return self.put_bytes(redact_text(content).encode("utf-8"), kind=kind)

    def put_bytes(self, content: bytes, *, kind: str) -> ArtifactRef:
        if not kind:
            raise ValueError("artifact kind 不能为空")
        digest = hashlib.sha256(content).hexdigest()
        relative = Path(digest[:2]) / digest
        destination = self._resolve_hash_path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)

        if not destination.exists():
            temp_path: str | None = None
            try:
                fd, temp_path = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=".artifact.",
                    suffix=".tmp",
                )
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, destination)
                temp_path = None
            finally:
                if temp_path is not None and os.path.exists(temp_path):
                    os.remove(temp_path)

        return ArtifactRef(
            artifact_id=digest,
            kind=kind,
            sha256=digest,
            size_bytes=len(content),
            relative_path=relative.as_posix(),
        )

    def read(self, reference: ArtifactRef) -> bytes:
        if reference.artifact_id != reference.sha256:
            raise ValueError("artifact id/hash 不一致")
        data = self._resolve_hash_path(reference.artifact_id).read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ValueError("artifact 内容 hash 不匹配")
        return data

    def _resolve_hash_path(self, digest: str) -> Path:
        if not _HASH.fullmatch(digest):
            raise ValueError("artifact id 必须是 SHA-256")
        candidate = (self.root / digest[:2] / digest).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("artifact path 逃逸 root") from exc
        return candidate
