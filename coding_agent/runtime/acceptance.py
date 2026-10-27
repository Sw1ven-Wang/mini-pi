"""Host-owned acceptance requirements, registered checks and versioned evidence."""

from __future__ import annotations

from enum import Enum
import fnmatch
import hashlib
import os
from pathlib import Path
from uuid import uuid4
from typing import Literal

from pydantic import Field, model_validator

from coding_agent.runtime.models import RuntimeValue, RuntimeConflictError
from coding_agent.runtime.store import RuntimeStore, digest
from coding_agent.runtime.privacy import is_link


class CheckDefinition(RuntimeValue):
    check_id: str
    definition_digest: str
    replay_safe: bool = False


class Requirement(RuntimeValue):
    requirement_id: str
    description: str
    required: bool = True
    check_ids: list[str] = Field(default_factory=list)
    manual_confirmation: bool = False

    @model_validator(mode="after")
    def has_check(self):
        if self.required and not self.check_ids and not self.manual_confirmation:
            raise ValueError("required requirement needs a check or human confirmation")
        return self


class AcceptanceInvariant(RuntimeValue):
    invariant_id: str
    kind: Literal["forbid_mutation", "protected_paths", "max_changed_files"]
    paths: list[str] = Field(default_factory=list)
    max_files: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_invariant(self):
        if self.kind == "max_changed_files" and self.max_files is None:
            raise ValueError("max_changed_files requires a limit")
        if self.kind != "max_changed_files" and not self.paths:
            raise ValueError("path invariant requires targets")
        return self


class AcceptanceContract(RuntimeValue):
    contract_id: str = Field(default_factory=lambda: str(uuid4()))
    revision: int = Field(default=1, gt=0)
    source: str = "host"
    coverage: str = "registered_checks_only"
    frozen: bool = True
    requirements: list[Requirement]
    checks: list[CheckDefinition]
    allowed_change_scope: list[str] = Field(default_factory=lambda: ["**"])
    invariants: list[AcceptanceInvariant] = Field(default_factory=lambda: [AcceptanceInvariant(
        invariant_id="control_plane", kind="forbid_mutation",
        paths=[".git", ".git/**", ".mini_pi", ".mini_pi/**"])])

    @model_validator(mode="after")
    def validate_bindings(self):
        ids = [item.check_id for item in self.checks]
        requirement_ids = [item.requirement_id for item in self.requirements]
        if not ids or not self.requirements or not any(item.required for item in self.requirements):
            raise ValueError("empty acceptance is not success")
        if len(ids) != len(set(ids)) or len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("duplicate acceptance ids")
        if any(check not in ids for item in self.requirements for check in item.check_ids):
            raise ValueError("unregistered acceptance check")
        patterns = [*self.allowed_change_scope, *(path for item in self.invariants for path in item.paths)]
        if not self.allowed_change_scope or any(not path or Path(path).is_absolute()
                or ".." in Path(path).parts or "\\" in path or ":" in path for path in patterns):
            raise ValueError("acceptance scope must use relative POSIX glob patterns")
        if len({item.invariant_id for item in self.invariants}) != len(self.invariants):
            raise ValueError("duplicate acceptance invariant")
        return self


class EvidenceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    MISSING = "missing"


class AcceptanceEvidence(RuntimeValue):
    evidence_id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    contract_id: str
    contract_revision: int
    check_id: str
    definition_digest: str
    workspace_digest: str
    passed: bool


class AcceptanceResult(RuntimeValue):
    passed: bool
    coverage: str
    requirements: dict[str, EvidenceStatus]
    pending_manual: list[str] = Field(default_factory=list)


class AcceptanceEvaluator:
    def evaluate(self, contract: AcceptanceContract, *, run_id: str,
                 workspace_digest: str, evidence: list[AcceptanceEvidence],
                 confirmations: dict[str, str] | None = None) -> AcceptanceResult:
        checks = {item.check_id: item for item in contract.checks}
        latest: dict[str, AcceptanceEvidence] = {}
        for item in evidence:
            check = checks.get(item.check_id)
            if (check and item.run_id == run_id and item.contract_id == contract.contract_id
                    and item.contract_revision == contract.revision
                    and item.definition_digest == check.definition_digest
                    and item.workspace_digest == workspace_digest):
                latest[item.check_id] = item
        requirements = {}
        pending = []
        for item in contract.requirements:
            values = [latest.get(check) for check in item.check_ids]
            manual = not item.manual_confirmation or (confirmations or {}).get(item.requirement_id) == workspace_digest
            if not manual:
                pending.append(item.requirement_id)
            if any(value is not None and not value.passed for value in values):
                state = EvidenceStatus.FAILED
            elif not manual or any(value is None for value in values):
                state = EvidenceStatus.MISSING
            else:
                state = EvidenceStatus.PASSED
            requirements[item.requirement_id] = state
        return AcceptanceResult(
            passed=contract.frozen and all(requirements[item.requirement_id] == EvidenceStatus.PASSED
                                          for item in contract.requirements if item.required),
            coverage=contract.coverage, requirements=requirements, pending_manual=pending,
        )


class AcceptanceService:
    def __init__(self, store: RuntimeStore, run_id: str):
        self.store, self.run_id = store, run_id

    def install(self, contract: AcceptanceContract) -> None:
        previous = self.store.get("contract", self.run_id)
        if previous and previous != contract.model_dump(mode="json"):
            raise RuntimeConflictError("frozen contract cannot be changed by a planner")
        self.store.put("contract", self.run_id, contract.model_dump(mode="json"), self.run_id)

    def contract(self) -> AcceptanceContract:
        return AcceptanceContract.model_validate(self.store.get("contract", self.run_id))

    def record(self, check_id: str, passed: bool, workspace_digest: str) -> AcceptanceEvidence:
        contract = self.contract()
        check = next((item for item in contract.checks if item.check_id == check_id), None)
        if check is None:
            raise RuntimeConflictError("unregistered acceptance check")
        evidence = AcceptanceEvidence(run_id=self.run_id, contract_id=contract.contract_id,
            contract_revision=contract.revision, check_id=check_id,
            definition_digest=check.definition_digest, workspace_digest=workspace_digest, passed=passed)
        self.store.put("evidence", evidence.evidence_id, evidence.model_dump(mode="json"), self.run_id)
        return evidence

    def evaluate(self, workspace_digest: str) -> AcceptanceResult:
        confirmations = self.store.get("confirmations", self.run_id) or {}
        return AcceptanceEvaluator().evaluate(self.contract(), run_id=self.run_id,
            workspace_digest=workspace_digest,
            evidence=[AcceptanceEvidence.model_validate(value) for value in self.store.values("evidence", self.run_id)],
            confirmations=confirmations)

    def confirm(self, requirement_id: str, workspace_digest: str) -> None:
        item = next((item for item in self.contract().requirements if item.requirement_id == requirement_id), None)
        if item is None or not item.manual_confirmation:
            raise RuntimeConflictError("not a manual acceptance requirement")
        values = self.store.get("confirmations", self.run_id) or {}
        values[requirement_id] = workspace_digest
        self.store.put("confirmations", self.run_id, values, self.run_id)

    def validate_mutations(self, paths: list[str], previously_changed: list[str]) -> None:
        contract = self.contract()
        for path in paths:
            if not any(_path_matches(path, pattern) for pattern in contract.allowed_change_scope):
                raise RuntimeConflictError("mutation is outside the acceptance scope")
            for invariant in contract.invariants:
                if invariant.kind in {"forbid_mutation", "protected_paths"} and any(
                        _path_matches(path, pattern) for pattern in invariant.paths):
                    raise RuntimeConflictError(f"mutation violates acceptance invariant: {invariant.invariant_id}")
        count = len(set([*previously_changed, *paths]))
        if any(item.kind == "max_changed_files" and count > item.max_files for item in contract.invariants):
            raise RuntimeConflictError("mutation exceeds acceptance file limit")

    def protected_fingerprint(self, workspace) -> str:
        patterns = [path for item in self.contract().invariants if item.kind == "protected_paths" for path in item.paths]
        if not patterns:
            return digest([])
        manifest = []
        visited = 0
        total = 0
        for current, directories, files in os.walk(workspace.root, followlinks=False):
            for name in sorted([*directories, *files]):
                visited += 1
                if visited > 20_000:
                    raise RuntimeConflictError("protected path manifest exceeds entry limit")
                path = Path(current) / name
                relative = path.relative_to(workspace.root).as_posix()
                if not any(_path_matches(relative, pattern) for pattern in patterns):
                    continue
                from coding_agent.runtime.journal import safe_file
                if is_link(path):
                    raise RuntimeConflictError("linked protected manifest entry")
                if path.is_dir():
                    continue
                target = safe_file(workspace, relative)
                total += target.stat().st_size
                if total > 128 * 1024 * 1024:
                    raise RuntimeConflictError("protected path manifest exceeds byte limit")
                manifest.append((relative, hashlib.sha256(target.read_bytes()).hexdigest()))
        return digest(sorted(manifest))


def _path_matches(path: str, pattern: str) -> bool:
    if os.name == "nt":
        path, pattern = path.lower(), pattern.lower()
    return fnmatch.fnmatchcase(path, pattern)


def default_contract(command: str) -> AcceptanceContract:
    return AcceptanceContract(
        checks=[CheckDefinition(check_id="verification_pipeline", definition_digest=digest({"command": command}))],
        requirements=[Requirement(requirement_id="registered_verification", description="Host-registered verification checks pass", check_ids=["verification_pipeline"])],
    )


# Generated caches do not constitute checked source. No arbitrary caller exclusions.
_CACHE_DIRS = {".git", ".mini_pi", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tmp", ".tmp_ci", "test-results", "playwright-report"}


def workspace_fingerprint(workspace, *, max_files: int = 20_000,
                          max_bytes: int = 128 * 1024 * 1024, include_paths: list[str] | None = None) -> str:
    """Bounded host-only source manifest; never return source or absolute paths."""
    manifest = []
    total = 0
    for current, directories, files in os.walk(workspace.root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in _CACHE_DIRS and not name.startswith(".tmp_v11"))
        for name in [*directories, *sorted(files)]:
            path = Path(current) / name
            if is_link(path):
                raise RuntimeConflictError("linked source manifest entry")
            relative = path.relative_to(workspace.root).as_posix()
            resolved = workspace.resolve(relative)
            if resolved.is_dir():
                continue
            total += resolved.stat().st_size
            if len(manifest) >= max_files or total > max_bytes:
                raise RuntimeConflictError("acceptance source manifest exceeds configured limit")
            manifest.append((relative, hashlib.sha256(resolved.read_bytes()).hexdigest()))
    known = {relative for relative, _ in manifest}
    for relative in sorted(set(include_paths or []) - known):
        from coding_agent.runtime.journal import safe_file
        target = safe_file(workspace, relative)
        if target.exists():
            total += target.stat().st_size
            if len(manifest) >= max_files or total > max_bytes:
                raise RuntimeConflictError("acceptance changed-source manifest exceeds limit")
            manifest.append((relative, hashlib.sha256(target.read_bytes()).hexdigest()))
        else:
            manifest.append((relative, None))
    return digest(sorted(manifest))
