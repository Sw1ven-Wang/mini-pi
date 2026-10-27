"""Repository navigation result models."""

from __future__ import annotations

from pydantic import BaseModel


class GlobMatch(BaseModel):
    path: str
    is_file: bool
    size_bytes: int | None = None


class GlobResult(BaseModel):
    pattern: str
    matches: list[GlobMatch]
    truncated: bool = False


class SearchMatch(BaseModel):
    path: str
    line: int
    column: int | None = None
    preview: str


class SearchResult(BaseModel):
    query: str
    matches: list[SearchMatch]
    truncated: bool = False
