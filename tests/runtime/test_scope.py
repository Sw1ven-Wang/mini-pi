from types import SimpleNamespace

import pytest

from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.filesystem.workspace import Workspace
from coding_agent.runtime.acceptance import AcceptanceInvariant, default_contract, workspace_fingerprint
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.runtime.models import RecoveryRequiredError, RuntimeConflictError
from coding_agent.safety.permissions import CodingPermissionEngine


def kernel(tmp_path, contract):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    value = RuntimeKernel(Workspace(workspace), state_root=tmp_path / "state")
    execution = value.start("s", command="test", contract=contract)
    execution.permission_engine = CodingPermissionEngine()
    return value, execution


@pytest.mark.parametrize("path", [".git/HEAD", ".mini_pi/settings.json"])
def test_default_control_plane_invariant_prevents_proposal(tmp_path, path):
    value, execution = kernel(tmp_path, default_contract("test"))
    try:
        with pytest.raises(RuntimeConflictError):
            execution.prepare_proposal(WorkspaceChangeSet(operations=[CreateFileOp(path=path, content="unsafe")]))
        assert execution.store.values("operation", execution.run_id) == []
    finally:
        value.close()


def test_scope_and_changed_file_limit(tmp_path):
    contract = default_contract("test")
    contract.allowed_change_scope = ["src/**"]
    contract.invariants.append(AcceptanceInvariant(invariant_id="size", kind="max_changed_files", max_files=1))
    value, execution = kernel(tmp_path, contract)
    try:
        with pytest.raises(RuntimeConflictError):
            execution.acceptance.validate_mutations(["tests/test_app.py"], [])
        execution.acceptance.validate_mutations(["src/app.py"], [])
        with pytest.raises(RuntimeConflictError):
            execution.acceptance.validate_mutations(["src/other.py"], ["src/app.py"])
    finally:
        value.close()


def test_verifier_cannot_modify_protected_check_source(tmp_path):
    contract = default_contract("test")
    contract.invariants.append(AcceptanceInvariant(invariant_id="golden", kind="protected_paths", paths=["golden.py"]))
    value, execution = kernel(tmp_path, contract)
    try:
        (execution.workspace.root / "golden.py").write_text("original", encoding="utf-8")
        fingerprint = execution.begin_verification()
        (execution.workspace.root / "golden.py").write_text("weakened", encoding="utf-8")
        with pytest.raises(RecoveryRequiredError):
            execution.verification_complete(SimpleNamespace(passed=True), fingerprint)
        assert execution.store.values("evidence", execution.run_id) == []
    finally:
        value.close()


def test_changed_files_in_generated_directories_still_bind_evidence(tmp_path):
    workspace = Workspace(tmp_path)
    (tmp_path / "__pycache__").mkdir()
    file = tmp_path / "__pycache__/requested.py"
    file.write_text("before", encoding="utf-8")
    first = workspace_fingerprint(workspace, include_paths=["__pycache__/requested.py"])
    file.write_text("after", encoding="utf-8")
    assert workspace_fingerprint(workspace, include_paths=["__pycache__/requested.py"]) != first
