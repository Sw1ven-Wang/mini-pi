from __future__ import annotations

import asyncio

import pytest

from agent.approvals import ApprovalDecision, ApprovalRequest
from coding_agent.changes.workspace_ir import CreateFileOp, WorkspaceChangeSet
from coding_agent.mutation.planner import plan_workspace_change_set
from coding_agent.mutation.preview import build_mutation_preview
from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.web.approval import WebApprovalHandler
from coding_agent.web.events import WebEventSink
from coding_agent.web.previews import InMemoryPreviewStore
from coding_agent.filesystem.workspace import Workspace


async def test_web_approval_handler_supports_once_session_and_expiry():
    changes = []

    async def on_change(value):
        changes.append(value)

    handler = WebApprovalHandler(on_change)
    waiter = asyncio.create_task(handler.request(ApprovalRequest(
        tool_name="workspace_mutation",
        arguments={},
        reason="write requested",
        rule_id="default_mutation_ask",
        risk="write",
        normalized_targets=["src/app.py"],
        preview_id="preview-1",
    )))
    await asyncio.sleep(0)
    pending = handler.pending
    assert pending is not None
    assert pending.preview_id == "preview-1"
    handler.resolve(pending.approval_id, ApprovalDecision.APPROVE_SESSION)
    assert await waiter == ApprovalDecision.APPROVE_SESSION
    assert changes[0] is not None and changes[-1] is None
    with pytest.raises(KeyError):
        handler.resolve(pending.approval_id, ApprovalDecision.APPROVE_ONCE)


async def test_web_approval_cancel_resolves_as_deny():
    async def on_change(_):
        pass

    handler = WebApprovalHandler(on_change)
    waiter = asyncio.create_task(handler.request(ApprovalRequest(
        tool_name="edit", arguments={}, reason="write", rule_id="ask",
    )))
    await asyncio.sleep(0)
    handler.deny_pending()
    assert await waiter == ApprovalDecision.DENY


async def test_event_sink_replays_after_sequence_and_redacts_secrets():
    sink = WebEventSink(max_events=3)
    for index in range(4):
        await sink.emit(AgentEvent(
            session_id="s",
            trace_id="t",
            span_id="p",
            type=AgentEventType.POLICY_EVALUATED,
            payload={"index": index, "api_key": "sk-secret-value"},
        ))
    await sink.close()
    replay = [item async for item in sink.stream(after_sequence=2)]
    assert [sequence for sequence, _ in replay] == [3, 4]
    assert all("sk-secret-value" not in str(event.payload) for _, event in replay)


async def test_event_sink_slow_subscriber_is_bounded():
    sink = WebEventSink(max_events=10, subscriber_queue_size=1)
    iterator = sink.stream().__aiter__()
    pending = asyncio.create_task(iterator.__anext__())
    await asyncio.sleep(0)
    await sink.emit(AgentEvent(
        session_id="s", trace_id="t", span_id="p",
        type=AgentEventType.TOOL_STARTED, payload={"index": 0},
    ))
    first = await pending
    for index in range(1, 3):
        await sink.emit(AgentEvent(
            session_id="s", trace_id="t", span_id="p",
            type=AgentEventType.TOOL_STARTED, payload={"index": index},
        ))
    latest = await iterator.__anext__()
    assert first[1].payload["index"] == 0
    assert latest[1].payload["index"] == 2
    await sink.close()


async def test_mutation_preview_is_ephemeral_and_contains_create_diff(tmp_path):
    workspace = Workspace(tmp_path)
    planned = plan_workspace_change_set(workspace, WorkspaceChangeSet(
        operations=[CreateFileOp(path="new.py", content="print('hello')\n")]
    ))
    preview = build_mutation_preview(planned)
    store = InMemoryPreviewStore(max_previews=1)
    await store.publish(preview)
    restored = store.get(preview.preview_id)
    assert restored is not None
    assert restored.operations[0].kind == "create_file"
    assert "+print('hello')" in restored.operations[0].diff
    store.clear()
    assert store.get(preview.preview_id) is None
