"""Deterministic, read-only evidence gathering for the V7 Viewer phase."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field
from agent.permissions import PermissionDecisionType, PermissionEngine
from coding_agent.errors import PermissionDeniedError
from coding_agent.tools.read import ReadTool

from coding_agent.agents.models import (
    EvidenceBundle,
    EvidenceSnippet,
    TaskPlan,
)
from coding_agent.filesystem.snapshots import FileSnapshot, snapshot_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.observations import (
    ObservationSource,
    ObservationStore,
    ObservedFileState,
)
from coding_agent.search.glob import GlobRuntime
from coding_agent.search.models import SearchMatch
from coding_agent.search.text_search import TextSearchRuntime
from coding_agent.task_budget import BudgetTracker


class ViewerLimits(BaseModel):
    """Hard bounds for one evidence-gathering phase."""

    max_files: int = Field(default=20, ge=1, le=20)
    max_snippets: int = Field(default=50, ge=1, le=50)
    max_total_chars: int = Field(default=100_000, ge=1, le=200_000)
    context_lines: int = Field(default=2, ge=0, le=10)


class Viewer:
    """Gather the smallest useful evidence set without mutation capability."""

    ALLOWED_OPERATIONS = frozenset({"glob", "search", "read"})

    def __init__(
        self,
        workspace: Workspace,
        *,
        observation_store: ObservationStore | None = None,
        limits: ViewerLimits | None = None,
        glob_runtime: GlobRuntime | None = None,
        search_runtime: TextSearchRuntime | None = None,
        budget_tracker: BudgetTracker | None = None,
        permission_engine: PermissionEngine | None = None,
    ) -> None:
        self.workspace = workspace
        self.observation_store = observation_store or ObservationStore()
        self.limits = limits or ViewerLimits()
        self.glob_runtime = glob_runtime or GlobRuntime(workspace)
        self.search_runtime = search_runtime or TextSearchRuntime(
            workspace,
            glob_runtime=self.glob_runtime,
        )
        self.budget_tracker = budget_tracker
        self.permission_engine = permission_engine

    async def readable_paths(self) -> frozenset[str] | None:
        if self.permission_engine is None:
            return None
        listing = await self.glob_runtime.glob("**/*", max_results=10_000)
        allowed = set()
        for item in listing.matches:
            if item.is_file and await self._can_read(item.path):
                allowed.add(item.path)
        return frozenset(allowed)

    async def _can_read(self, path: str) -> bool:
        if self.permission_engine is None:
            return True
        decision = await self.permission_engine.evaluate(ReadTool(self.workspace), {"path": path})
        return decision.decision == PermissionDecisionType.ALLOW

    async def gather(
        self,
        plan: TaskPlan,
        *,
        repository_notes: list[str] | None = None,
    ) -> EvidenceBundle:
        unresolved: list[str] = []
        direct_paths = await self._resolve_targets(plan, unresolved)
        search_matches = await self._run_queries(plan, unresolved)

        candidate_paths = list(direct_paths)
        for match in search_matches:
            if match.path not in candidate_paths:
                candidate_paths.append(match.path)

        if len(candidate_paths) > self.limits.max_files:
            unresolved.append(
                "Evidence file limit reached; some relevant files were not read."
            )
        selected_paths = candidate_paths[: self.limits.max_files]
        selected_set = set(selected_paths)
        selected_matches = [
            match for match in search_matches if match.path in selected_set
        ][: self.limits.max_snippets]

        snapshots: dict[str, FileSnapshot] = {}
        for path in selected_paths:
            if not await self._can_read(path):
                raise PermissionDeniedError(tool="read", reason="Viewer evidence path is not authorized")
            try:
                snapshots[path] = self._read(path)
            except (OSError, UnicodeError, ValueError) as exc:
                unresolved.append(f"Could not read {path}: {exc}")

        snippets = self._build_snippets(
            snapshots=snapshots,
            direct_paths=set(direct_paths),
            search_matches=selected_matches,
            unresolved=unresolved,
        )

        if not snippets:
            unresolved.append("No readable evidence matched the current plan.")

        return EvidenceBundle(
            snippets=snippets,
            search_results=selected_matches,
            repository_notes=list(repository_notes or []),
            unresolved_questions=_deduplicate(unresolved),
        )

    async def _resolve_targets(
        self,
        plan: TaskPlan,
        unresolved: list[str],
    ) -> list[str]:
        paths: list[str] = []

        for target in _unique(
            path for step in plan.steps for path in step.target_files
        ):
            if any(char in target for char in "*?["):
                if self.budget_tracker is not None:
                    self.budget_tracker.consume_tool_call()
                try:
                    result = await self.glob_runtime.glob(
                        target,
                        max_results=self.limits.max_files + 1,
                    )
                except (OSError, ValueError) as exc:
                    unresolved.append(f"Could not glob {target}: {exc}")
                    continue

                matches = [item.path for item in result.matches if item.is_file]
                paths.extend(matches)
                if result.truncated or len(matches) > self.limits.max_files:
                    unresolved.append(f"Target pattern was truncated: {target}")
                if not matches:
                    unresolved.append(f"Target pattern matched no files: {target}")
                continue

            try:
                absolute = self.workspace.resolve(target)
            except (OSError, ValueError) as exc:
                unresolved.append(f"Invalid target path {target}: {exc}")
                continue

            if absolute.is_file():
                paths.append(self.workspace.relative(absolute))
            else:
                unresolved.append(f"Target file was not found: {target}")

        return _unique(paths)

    async def _run_queries(
        self,
        plan: TaskPlan,
        unresolved: list[str],
    ) -> list[SearchMatch]:
        matches: list[SearchMatch] = []
        allowed_paths = await self.readable_paths()

        for query in _unique(
            query for step in plan.steps for query in step.evidence_queries
        ):
            remaining = self.limits.max_snippets - len(matches)
            if remaining <= 0:
                unresolved.append(
                    "Evidence snippet limit reached before all queries ran."
                )
                break

            try:
                if self.budget_tracker is not None:
                    self.budget_tracker.consume_tool_call()
                result = await self.search_runtime.search_text(
                    query,
                    max_results=remaining,
                    context_lines=self.limits.context_lines,
                    allowed_paths=allowed_paths,
                )
            except (OSError, ValueError) as exc:
                unresolved.append(f"Could not search for {query!r}: {exc}")
                continue

            matches.extend(result.matches)
            if result.truncated:
                unresolved.append(f"Search results were truncated: {query}")
            if not result.matches:
                unresolved.append(f"Search returned no matches: {query}")

        return matches[: self.limits.max_snippets]

    def _read(self, path: str) -> FileSnapshot:
        if self.budget_tracker is not None:
            self.budget_tracker.consume_tool_call()
        absolute = self.workspace.resolve(path)
        snapshot = snapshot_file(absolute, workspace_relative=path)
        self.observation_store.update(
            ObservedFileState(
                path=path,
                exists=True,
                version=snapshot.version,
                source=ObservationSource.READ,
                observed_at=datetime.now(timezone.utc),
            )
        )
        return snapshot

    def _build_snippets(
        self,
        *,
        snapshots: dict[str, FileSnapshot],
        direct_paths: set[str],
        search_matches: list[SearchMatch],
        unresolved: list[str],
    ) -> list[EvidenceSnippet]:
        snippets: list[EvidenceSnippet] = []
        used_chars = 0
        covered_paths: set[str] = set()

        for path in snapshots:
            if path not in direct_paths:
                continue
            snapshot = snapshots[path]
            content, was_truncated = _fit_text(
                snapshot.content,
                self.limits.max_total_chars - used_chars,
            )
            if content == "" and snapshot.content:
                unresolved.append("Evidence character limit reached.")
                break
            snippets.append(
                EvidenceSnippet(
                    path=path,
                    start_line=1,
                    end_line=max(1, content.count("\n") + 1),
                    content=content,
                    version=snapshot.version,
                )
            )
            covered_paths.add(path)
            used_chars += len(content)
            if was_truncated:
                unresolved.append(f"File evidence was truncated: {path}")
            if len(snippets) >= self.limits.max_snippets:
                unresolved.append("Evidence snippet limit reached.")
                return snippets

        seen_ranges: set[tuple[str, int, int]] = set()
        for match in search_matches:
            if match.path in covered_paths or match.path not in snapshots:
                continue
            snapshot = snapshots[match.path]
            lines = snapshot.content.splitlines()
            start = max(1, match.line - self.limits.context_lines)
            end = min(max(1, len(lines)), match.line + self.limits.context_lines)
            key = (match.path, start, end)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            raw_content = "\n".join(lines[start - 1 : end])
            content, was_truncated = _fit_text(
                raw_content,
                self.limits.max_total_chars - used_chars,
            )
            if content == "" and raw_content:
                unresolved.append("Evidence character limit reached.")
                break
            snippets.append(
                EvidenceSnippet(
                    path=match.path,
                    start_line=start,
                    end_line=end,
                    content=content,
                    version=snapshot.version,
                )
            )
            used_chars += len(content)
            if was_truncated:
                unresolved.append(f"Snippet evidence was truncated: {match.path}")
            if len(snippets) >= self.limits.max_snippets:
                unresolved.append("Evidence snippet limit reached.")
                break

        return snippets


def _fit_text(text: str, remaining: int) -> tuple[str, bool]:
    if len(text) <= remaining:
        return text, False
    if remaining <= 0:
        return "", bool(text)
    return text[:remaining], True


def _unique(values) -> list[str]:
    return list(dict.fromkeys(values))


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
