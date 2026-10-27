from pathlib import Path

from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.observability.metrics import MetricsSink
from coding_agent.replay.recorder import TraceRecorder


async def test_trace_recorder_redacts_and_persists_atomically(tmp_path: Path):
    metrics = MetricsSink()
    recorder = TraceRecorder(metrics)
    await recorder.emit(AgentEvent(
        session_id="session", trace_id="trace", span_id="span",
        type=AgentEventType.TASK_STARTED,
        payload={"token": "secret-value", "message": "Bearer abcdefghijklmnop"},
    ))
    await recorder.emit(AgentEvent(
        session_id="session", trace_id="trace", span_id="span",
        type=AgentEventType.TASK_COMPLETED,
        payload={"passed": True, "duration_ms": 12},
    ))
    recorder.record_action(
        WorkspaceChangeSet(operations=[CreateFileOp(path="new.txt", content="ok")]),
        safe_arguments={"target": "new.txt"},
    )
    record = recorder.finalize()
    destination = tmp_path / "trace.json"
    recorder.save(record, destination)
    text = destination.read_text(encoding="utf-8")
    assert "secret-value" not in text
    assert "abcdefghijklmnop" not in text
    assert record.metrics.task_success is True
    assert record.tool_actions[0].safe_arguments == {"target": "new.txt"}
