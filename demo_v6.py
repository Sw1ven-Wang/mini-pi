"""Mini-Pi V6 interactive demo: observe, compact, persist and resume.

Start a new session:
    uv run python demo_v6.py

Resume a previous session without replaying pending mutations:
    $env:MINI_PI_SESSION_ID = "<printed session id>"
    uv run python demo_v6.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ai.models import Model
from ai.providers.openai import OpenAIProvider
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry
from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.context_budget import ContextBudget
from coding_agent.context_manager import ContextManager
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import powershell_config
from coding_agent.safety.approvals import CLIApprovalHandler
from coding_agent.safety.factory import permission_engine_from_workspace
from coding_agent.sandbox.factory import sandbox_from_settings
from coding_agent.session import CodingSession
from coding_agent.session_store import SessionStore
from coding_agent.tools.delete import DeleteTool
from coding_agent.tools.edit import EditTool
from coding_agent.tools.glob import GlobTool
from coding_agent.tools.move import MoveTool
from coding_agent.tools.patch import PatchTool
from coding_agent.tools.powershell import PowerShellTool
from coding_agent.tools.read import ReadTool
from coding_agent.tools.search import SearchTool
from coding_agent.tools.write import WriteTool


USER_GOAL = "修复当前项目中的失败测试，并在验证通过后总结修改。"


def _dispatcher(
    workspace: Workspace,
    provider: OpenAIProvider,
    model: Model,
) -> ToolDispatcher:
    mutation_queue = FileMutationQueue()
    checkpoint_store = CheckpointStore()
    process_runner = ProcessRunner()
    permission_engine = permission_engine_from_workspace(
        workspace.root, provider=provider, model=model,
    )
    tools = [
        GlobTool(workspace),
        SearchTool(workspace),
        ReadTool(workspace),
        WriteTool(workspace, mutation_queue, checkpoint_store),
        EditTool(workspace, mutation_queue, checkpoint_store),
        PatchTool(workspace, mutation_queue, checkpoint_store),
        MoveTool(workspace, mutation_queue, checkpoint_store),
        DeleteTool(workspace, mutation_queue, checkpoint_store),
        PowerShellTool(
            workspace,
            process_runner,
            sandbox=sandbox_from_settings(
                permission_engine.settings.sandbox,
                process_runner=process_runner,
                shell_config=powershell_config(),
            ),
        ),
    ]
    return ToolDispatcher(
        ToolRegistry(tools),
        permission_engine,
        approval_handler=CLIApprovalHandler(),
    )


async def main() -> None:
    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError("请先设置 DEEPSEEK_API_KEY")

    root = Path.cwd()
    provider = OpenAIProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )
    model = Model(id="deepseek-v4-flash", provider="deepseek")
    session_store = SessionStore(root / ".mini_pi" / "sessions")
    context_manager = ContextManager(
        budget=ContextBudget(
            max_tokens=64_000,
            reserve_tokens=8_192,
            compact_threshold_ratio=0.75,
        )
    )
    session_id = os.getenv("MINI_PI_SESSION_ID")

    if session_id:
        session = CodingSession.resume(
            session_id,
            provider=provider,
            model=model,
            tool_dispatcher=_dispatcher(Workspace(root), provider, model),
            session_store=session_store,
            context_manager=context_manager,
            max_turns=30,
        )
        user_message = "恢复任务；先核对当前磁盘状态，再继续未完成工作。"
    else:
        session = CodingSession.create(
            user_goal=USER_GOAL,
            provider=provider,
            model=model,
            tool_dispatcher=_dispatcher(Workspace(root), provider, model),
            session_store=session_store,
            context_manager=context_manager,
            system_prompt=(
                "你是 Windows 环境中的 coding agent。"
                "仓库定位优先 glob/search/read；修改使用专用 mutation 工具；"
                "PowerShell 只用于测试、lint 和 build。"
            ),
            max_turns=30,
        )
        user_message = USER_GOAL

    print(f"Session ID: {session.session_id}")
    final = await session.run(user_message)
    print(final.content)


if __name__ == "__main__":
    asyncio.run(main())
