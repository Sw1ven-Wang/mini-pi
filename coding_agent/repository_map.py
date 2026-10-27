"""Small, deterministic Python repository map for V7 navigation."""

from __future__ import annotations

import ast

from pydantic import BaseModel, Field

from coding_agent.filesystem.encoding import decode_text_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.search.glob import GlobRuntime


class RepositorySymbol(BaseModel):
    name: str = Field(min_length=1)
    line: int = Field(ge=1)


class RepositoryFile(BaseModel):
    path: str
    classes: list[RepositorySymbol] = Field(default_factory=list)
    functions: list[RepositorySymbol] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)


class RepositoryMap(BaseModel):
    """Navigation metadata only; source text deliberately is not represented."""

    files: list[RepositoryFile] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    truncated: bool = False

    def render_summary(self, *, max_chars: int = 20_000) -> str:
        lines: list[str] = []
        for item in self.files:
            parts: list[str] = []
            if item.classes:
                parts.append("classes=" + ",".join(symbol.name for symbol in item.classes))
            if item.functions:
                parts.append(
                    "functions=" + ",".join(symbol.name for symbol in item.functions)
                )
            if item.imports:
                parts.append("imports=" + ",".join(item.imports))
            suffix = f" | {'; '.join(parts)}" if parts else ""
            line = f"{item.path}{suffix}"
            if len("\n".join([*lines, line])) > max_chars:
                lines.append("... repository map summary truncated ...")
                break
            lines.append(line)

        lines.extend(f"note: {note}" for note in self.notes)
        return "\n".join(lines)[:max_chars]


class RepositoryMapBuilder:
    def __init__(
        self,
        workspace: Workspace,
        *,
        max_files: int = 500,
        max_file_bytes: int = 1_000_000,
        glob_runtime: GlobRuntime | None = None,
    ) -> None:
        if max_files <= 0:
            raise ValueError("max_files must be positive")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.workspace = workspace
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.glob_runtime = glob_runtime or GlobRuntime(workspace)
        self.allowed_paths: frozenset[str] | None = None

    def build(self, pattern: str = "**/*.py") -> RepositoryMap:
        listing = self.glob_runtime.glob_sync(
            pattern,
            max_results=self.max_files + 1,
        )
        file_paths = [item.path for item in listing.matches if item.is_file]
        truncated = listing.truncated or len(file_paths) > self.max_files
        notes: list[str] = []
        files: list[RepositoryFile] = []

        for path in file_paths[: self.max_files]:
            if self.allowed_paths is not None and path not in self.allowed_paths:
                continue
            absolute = self.workspace.resolve(path)
            try:
                if absolute.stat().st_size > self.max_file_bytes:
                    notes.append(f"Skipped symbols for oversized file: {path}")
                    files.append(RepositoryFile(path=path))
                    continue
                content = decode_text_file(absolute.read_bytes()).content
                tree = ast.parse(content, filename=path)
            except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
                notes.append(f"Could not parse {path}: {type(exc).__name__}")
                files.append(RepositoryFile(path=path))
                continue

            classes: list[RepositorySymbol] = []
            functions: list[RepositorySymbol] = []
            imports: list[str] = []
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    classes.append(RepositorySymbol(name=node.name, line=node.lineno))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions.append(RepositorySymbol(name=node.name, line=node.lineno))
                elif isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = "." * node.level + (node.module or "")
                    imported = ",".join(alias.name for alias in node.names)
                    imports.append(f"{module}:{imported}")

            files.append(
                RepositoryFile(
                    path=path,
                    classes=classes,
                    functions=functions,
                    imports=imports,
                )
            )

        if truncated:
            notes.append("Repository map file limit reached.")

        return RepositoryMap(files=files, notes=notes, truncated=truncated)
