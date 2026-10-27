from pathlib import Path

from coding_agent.filesystem.workspace import Workspace
from coding_agent.observability.artifacts import ArtifactStore
from coding_agent.observability.event_bus import EventBus, InMemoryEventSink
from coding_agent.observability.events import AgentEvent, AgentEventType
from coding_agent.sandbox.local import LocalSandbox


async def test_secret_environment_not_in_sandbox_process(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("MINI_PI_TEST_SECRET", "TOP_SECRET_VALUE")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "DEEPSEEK_SECRET_VALUE")
    session = await LocalSandbox().start(workspace=Workspace(tmp_path))
    result = await session.run("Get-ChildItem Env: | Out-String")
    await session.stop()
    output = result.stdout + result.stderr
    assert "TOP_SECRET_VALUE" not in output
    assert "DEEPSEEK_SECRET_VALUE" not in output
    assert "MINI_PI_TEST_SECRET" not in output
    assert "DEEPSEEK_API_KEY" not in output


async def test_secret_not_in_event_or_artifact(tmp_path: Path):
    sink = InMemoryEventSink()
    await EventBus([sink]).emit(
        AgentEvent(
            session_id="s",
            trace_id="t",
            span_id="p",
            type=AgentEventType.TASK_STARTED,
            payload={"DEEPSEEK_API_KEY": "TOP_SECRET_VALUE"},
        )
    )
    event_json = sink.events[0].model_dump_json()
    assert "TOP_SECRET_VALUE" not in event_json

    store = ArtifactStore(tmp_path / "artifacts")
    reference = store.put_text(
        "DEEPSEEK_API_KEY=TOP_SECRET_VALUE", kind="sandbox-log"
    )
    assert "TOP_SECRET_VALUE" not in reference.model_dump_json()
    assert b"TOP_SECRET_VALUE" not in store.read(reference)
