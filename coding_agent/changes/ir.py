"""Unified Change IR.

EditTool / PatchTool / 未来的 Planner 最终都落到统一 Change IR。
第一版只做 deterministic exact replacement，不做 AST operation。
"""

from __future__ import annotations

from pydantic import BaseModel

from coding_agent.filesystem.versions import FileVersion


class TextReplacement(BaseModel):
    old_text: str
    new_text: str


class FileChange(BaseModel):
    path: str
    expected_version: FileVersion | None = None
    replacements: list[TextReplacement]


class ChangeSet(BaseModel):
    files: list[FileChange]
