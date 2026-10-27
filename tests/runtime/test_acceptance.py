import pytest

from coding_agent.runtime.acceptance import (
    AcceptanceContract, AcceptanceEvidence, AcceptanceEvaluator, CheckDefinition, Requirement,
)


def contract():
    return AcceptanceContract(checks=[CheckDefinition(check_id="test", definition_digest="definition")],
        requirements=[Requirement(requirement_id="r", description="Test passes", check_ids=["test"])])


def evidence(value, **changes):
    return AcceptanceEvidence(run_id="run", contract_id=value.contract_id, contract_revision=value.revision,
        check_id="test", definition_digest="definition", workspace_digest="code", passed=True).model_copy(update=changes)


@pytest.mark.parametrize("changes", [{"run_id": "other"}, {"contract_revision": 2},
    {"check_id": "invented"}, {"definition_digest": "changed"}, {"workspace_digest": "old"}, {"passed": False}])
def test_stale_or_forged_evidence_not_success(changes):
    value = contract()
    result = AcceptanceEvaluator().evaluate(value, run_id="run", workspace_digest="code", evidence=[evidence(value, **changes)])
    assert not result.passed


def test_valid_evidence_and_missing_evidence():
    value = contract()
    evaluator = AcceptanceEvaluator()
    assert evaluator.evaluate(value, run_id="run", workspace_digest="code", evidence=[evidence(value)]).passed
    assert not evaluator.evaluate(value, run_id="run", workspace_digest="code", evidence=[]).passed


def test_manual_acceptance_binds_code():
    value = contract()
    value.requirements.append(Requirement(requirement_id="manual", description="User review", manual_confirmation=True))
    evaluator = AcceptanceEvaluator()
    assert not evaluator.evaluate(value, run_id="run", workspace_digest="code", evidence=[evidence(value)]).passed
    assert not evaluator.evaluate(value, run_id="run", workspace_digest="code", evidence=[evidence(value)], confirmations={"manual": "old"}).passed
    assert evaluator.evaluate(value, run_id="run", workspace_digest="code", evidence=[evidence(value)], confirmations={"manual": "code"}).passed


@pytest.mark.parametrize("changes", [{"checks": []}, {"requirements": []},
    {"requirements": [{"requirement_id": "r", "description": "unbound"}]},
    {"requirements": [{"requirement_id": "r", "description": "forged", "check_ids": ["unknown"]}]}])
def test_invalid_contracts_rejected(changes):
    with pytest.raises(ValueError):
        AcceptanceContract.model_validate({**contract().model_dump(), **changes})
