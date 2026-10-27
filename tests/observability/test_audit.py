import json
from pathlib import Path

from coding_agent.observability.audit import AuditSink
from coding_agent.observability.events import AgentEvent, AgentEventType


async def test_audit_records_decision_rule_without_secret_output(tmp_path: Path):
    sink = AuditSink(tmp_path / ".mini_pi" / "audit")
    await sink.emit(AgentEvent(
        session_id="session", trace_id="trace", span_id="span",
        type=AgentEventType.TOOL_DENIED,
        payload={
            "tool_name": "powershell", "target": "legacy.py", "decision": "deny",
            "rule_id": "remove-item", "reason": "dangerous", "token": "SECRET_VALUE",
        },
    ))
    audit_file = next((tmp_path / ".mini_pi" / "audit").glob("*.jsonl"))
    text = audit_file.read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["tool"] == "powershell"
    assert record["target"] == "legacy.py"
    assert record["rule_id"] == "remove-item"
    assert "SECRET_VALUE" not in text


async def test_audit_ignores_non_security_events(tmp_path: Path):
    sink = AuditSink(tmp_path / "audit")
    await sink.emit(AgentEvent(
        session_id="s", trace_id="t", span_id="p",
        type=AgentEventType.MODEL_REQUEST_COMPLETED,
    ))
    assert not (tmp_path / "audit").exists()


async def test_permission_audit_never_persists_raw_command_or_arguments(tmp_path: Path):
    sink = AuditSink(tmp_path / "audit")
    await sink.emit(AgentEvent(
        session_id="s", trace_id="t", span_id="p",
        type=AgentEventType.POLICY_EVALUATED,
        payload={
            "tool_name": "powershell",
            "permission_decision": "ask",
            "rule_id": "filesystem-write:redirection",
            "matched_rule_ids": ["filesystem-write:redirection"],
            "command_hash": "a" * 64,
            "operations": [{"risk": "write", "target": "result.txt"}],
            "normalized_targets": ["result.txt"],
            "raw_command": "Set-Content result.txt ULTRA_SECRET_VALUE",
            "arguments": {"command": "Set-Content result.txt ULTRA_SECRET_VALUE"},
        },
    ))
    text = next((tmp_path / "audit").glob("*.jsonl")).read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["target"] == "result.txt"
    assert record["details"]["command_hash"] == "a" * 64
    assert record["details"]["operations"][0]["risk"] == "write"
    assert "Set-Content" not in text
    assert "ULTRA_SECRET_VALUE" not in text
