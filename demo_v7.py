"""Run the single-model phase pipeline through the V11 RuntimeKernel.

Required PowerShell environment variables:
    $env:DEEPSEEK_API_KEY = "..."
    $env:MINI_PI_V7_WORKSPACE = "E:\\path\\to\\fixture-repo"

Optional:
    $env:MINI_PI_V7_GOAL = "Fix the failing tests"
    $env:MINI_PI_V7_VERIFY = "uv run python -m pytest -q"
    $env:DEEPSEEK_MODEL = "deepseek-chat"
    $env:MINI_PI_V11_RUN_ID = "..."  # explicitly resume an interrupted run
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ai.models import Model
from ai.providers.openai import OpenAIProvider
from coding_agent.filesystem.workspace import Workspace
from coding_agent.safety.approvals import CLIApprovalHandler
from coding_agent.runtime.kernel import RuntimeKernel
from coding_agent.runtime.factory import build_coding_runtime
from coding_agent.observability.event_bus import EventBus
from coding_agent.observability.tracing import TraceContext
from uuid import uuid4


async def main() -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    workspace_value = os.getenv("MINI_PI_V7_WORKSPACE")
    if not api_key:
        raise RuntimeError("请先设置 DEEPSEEK_API_KEY")
    if not workspace_value:
        raise RuntimeError("请显式设置 MINI_PI_V7_WORKSPACE，避免误改当前仓库")

    workspace = Workspace(Path(workspace_value))
    goal = os.getenv("MINI_PI_V7_GOAL", "修复失败测试并保持现有公开 API")
    provider = OpenAIProvider(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    model = Model(
        id=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        provider="deepseek",
    )
    kernel = RuntimeKernel(workspace)
    try:
        command = os.getenv("MINI_PI_V7_VERIFY", "uv run python -m pytest -q")
        run_id = os.getenv("MINI_PI_V11_RUN_ID")
        if run_id:
            kernel.mark_interrupted()
            execution = kernel.resume(run_id)
        else:
            execution = kernel.start(str(uuid4()), command=command, goal=goal)
        session, orchestrator = build_coding_runtime(execution=execution, provider=provider, model=model,
            approval_handler=CLIApprovalHandler(), verify_command=command,
            event_bus=EventBus(), trace_context=TraceContext.root(), resume=bool(run_id), goal=goal)
        print(f"Session ID: {session.session_id}; Run ID: {execution.run_id}")
        outcome = await kernel.execute(execution, orchestrator, session)
        print(outcome.model_dump_json(indent=2))
    finally:
        kernel.close()


if __name__ == "__main__":
    asyncio.run(main())
