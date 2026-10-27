from coding_agent.observability.events import AgentEventType
from coding_agent.observability.metrics import MetricsSink, RunMetrics
from coding_agent.observability.tracing import TraceContext


async def test_metrics_sink_collects_runtime_events():
    trace = TraceContext.root()
    sink = MetricsSink()
    for event_type, payload in [
        (AgentEventType.MODEL_REQUEST_COMPLETED, {"input_tokens": 10, "output_tokens": 3}),
        (AgentEventType.TOOL_REQUESTED, {"tool_name": "read"}),
        (AgentEventType.TOOL_COMPLETED, {"tool_name": "read"}),
        (AgentEventType.MUTATION_PLANNED, {}),
        (AgentEventType.VERIFICATION_STARTED, {}),
        (AgentEventType.VERIFICATION_COMPLETED, {"passed": False}),
        (AgentEventType.PLAN_REVISED, {}),
        (AgentEventType.AUTO_CLASSIFIED, {
            "rule_id": "auto_classifier_allow", "duration_ms": 17,
            "classifier_input_tokens": 20, "classifier_output_tokens": 2,
        }),
        (AgentEventType.SANDBOX_VIOLATION, {"kind": "network", "count": 3}),
        (AgentEventType.TASK_COMPLETED, {"passed": True, "duration_ms": 15}),
    ]:
        await sink.emit(trace.event(session_id="s", type=event_type, payload=payload))

    metrics = sink.snapshot(trace.trace_id)
    assert metrics.model_calls == 1
    assert metrics.read_calls == metrics.tool_calls == 1
    assert metrics.mutation_attempts == 1
    assert metrics.verification_runs == metrics.verification_failures == 1
    assert metrics.replans == 1
    assert metrics.task_success is True
    assert metrics.duration_ms == 15
    assert metrics.auto_classifier_calls == metrics.auto_allows == 1
    assert metrics.auto_classifier_latency_ms == 17
    assert metrics.auto_classifier_input_tokens == 20
    assert metrics.auto_classifier_output_tokens == 2
    assert metrics.network_blocks == metrics.sandbox_violations == 3


def test_run_metrics_contains_v7_and_v8_fields():
    fields = RunMetrics.model_fields
    assert {"input_tokens", "output_tokens", "editor_input_tokens", "files_read"} <= fields.keys()
    assert {"rollback_count", "process_timeouts", "sandbox_violations"} <= fields.keys()
    assert {"auto_classifier_latency_ms", "auto_classifier_input_tokens"} <= fields.keys()
