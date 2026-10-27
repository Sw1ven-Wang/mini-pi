"""Mini-Pi V5 E2E demo：Complete Filesystem Mutations。

验证完整的 Coding Agent 闭环：

1. glob / search / read 定位问题
2. move 重命名 legacy_math.py → math_utils.py
3. patch 更新 service.py import
4. delete 删除废弃的 unused.py
5. VerificationPipeline 运行 pytest
6. Host 独立验证最终状态
"""

import asyncio
import os
from pathlib import Path

from ai.context import Context
from ai.messages import UserMessage
from ai.models import Model
from ai.providers.openai import OpenAIProvider

from agent.agent_loop import (
    AgentLoopError,
    run_agent_loop,
)
from agent.tool_dispatcher import ToolDispatcher
from agent.tool_registry import ToolRegistry

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import powershell_config
from coding_agent.safety.approvals import CLIApprovalHandler
from coding_agent.safety.factory import permission_engine_from_workspace
from coding_agent.sandbox.factory import sandbox_from_settings
from coding_agent.tools.delete import DeleteTool
from coding_agent.tools.glob import GlobTool
from coding_agent.tools.move import MoveTool
from coding_agent.tools.patch import PatchTool
from coding_agent.tools.powershell import PowerShellTool
from coding_agent.tools.read import ReadTool
from coding_agent.tools.search import SearchTool
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.presets import pytest_verifier


def _reset_demo_project(demo_root: Path) -> None:
    """每次运行都恢复固定的 V5 演示项目状态。"""

    src = demo_root / "src"
    tests = demo_root / "tests"
    src.mkdir(exist_ok=True)
    tests.mkdir(exist_ok=True)

    (src / "legacy_math.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    (src / "service.py").write_text(
        "from src.legacy_math import add\n"
        "\n"
        "\n"
        "def total(a, b):\n"
        "    return add(a, b)\n",
        encoding="utf-8",
    )

    (src / "unused.py").write_text(
        "# deprecated\n",
        encoding="utf-8",
    )

    (tests / "test_service.py").write_text(
        "from src.service import total\n"
        "\n"
        "\n"
        "def test_total():\n"
        "    assert total(2, 3) == 5\n",
        encoding="utf-8",
    )


async def main() -> None:
    # ==================================================
    # 1. Demo Workspace + fixture
    # ==================================================

    project_root = Path.cwd()
    demo_root = project_root / ".mini_pi_demo"
    demo_root.mkdir(exist_ok=True)

    print(f"[Demo Workspace] {demo_root}")

    _reset_demo_project(demo_root)

    # ==================================================
    # 2. Provider
    # ==================================================

    api_key = os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        raise RuntimeError(
            "没有找到环境变量 DEEPSEEK_API_KEY"
        )

    provider = OpenAIProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    model = Model(
        id="deepseek-v4-flash",
        provider="deepseek",
    )

    # ==================================================
    # 3. 共享基础设施
    # ==================================================

    workspace = Workspace(demo_root)
    process_runner = ProcessRunner()
    mutation_queue = FileMutationQueue()
    checkpoint_store = CheckpointStore()
    permission_engine = permission_engine_from_workspace(
        demo_root, provider=provider, model=model,
    )
    approval_handler = CLIApprovalHandler()

    # ==================================================
    # 4. Tools
    # ==================================================

    glob_tool = GlobTool(workspace=workspace)
    search_tool = SearchTool(workspace=workspace)
    read_tool = ReadTool(workspace=workspace)
    patch_tool = PatchTool(
        workspace=workspace,
        mutation_queue=mutation_queue,
        checkpoint_store=checkpoint_store,
    )
    move_tool = MoveTool(
        workspace=workspace,
        mutation_queue=mutation_queue,
        checkpoint_store=checkpoint_store,
    )
    delete_tool = DeleteTool(
        workspace=workspace,
        mutation_queue=mutation_queue,
        checkpoint_store=checkpoint_store,
    )
    powershell_tool = PowerShellTool(
        workspace=workspace,
        process_runner=process_runner,
        sandbox=sandbox_from_settings(
            permission_engine.settings.sandbox,
            process_runner=process_runner,
            shell_config=powershell_config(),
        ),
    )

    # ==================================================
    # 5. Registry 和 Dispatcher
    # ==================================================

    registry = ToolRegistry(
        [
            glob_tool,
            search_tool,
            read_tool,
            patch_tool,
            move_tool,
            delete_tool,
            powershell_tool,
        ]
    )

    dispatcher = ToolDispatcher(
        registry=registry,
        permission_engine=permission_engine,
        approval_handler=approval_handler,
    )

    # ==================================================
    # 6. Context
    # ==================================================

    context = Context(
        system_prompt=(
            "你是一个 coding agent。"
            "当前环境是 Windows。"
            "创建新文件优先使用 write。"
            "修改已有文件优先使用 edit/patch。"
            "删除文件必须使用 delete，不要使用 PowerShell Remove-Item。"
            "移动或重命名文件必须使用 move，不要使用 PowerShell Move-Item。"
            "仓库定位优先 glob/search/read。"
            "PowerShell 主要用于测试、lint、build 等命令。"
            "执行命令必须使用 powershell 工具，"
            "不要使用 Bash/Linux 专属命令。"
        ),
        messages=[
            UserMessage(
                content=(
                    "将 legacy_math.py 重命名为 math_utils.py，"
                    "更新所有引用，"
                    "删除明确标记为废弃的 unused.py，"
                    "然后运行全部测试。\n"
                    "要求：\n"
                    "1. 使用 glob 定位所有 Python 文件。\n"
                    "2. 使用 search 搜索 legacy_math 的引用。\n"
                    "3. 使用 read 读取相关文件获取版本。\n"
                    "4. 使用 move 重命名 legacy_math.py。\n"
                    "5. 使用 patch 更新 service.py 的 import。\n"
                    "6. 使用 delete 删除 unused.py。\n"
                    "7. 使用 powershell 运行："
                    "uv run python -m pytest -q\n"
                    "8. 只有测试通过后才报告完成。"
                )
            ),
        ],
    )

    # ==================================================
    # 7. Agent Loop
    # ==================================================

    print()
    print("[Agent Start]")
    print()

    try:
        final_message = await run_agent_loop(
            provider=provider,
            model=model,
            context=context,
            tool_dispatcher=dispatcher,
            max_turns=25,
        )
    except AgentLoopError as exc:
        print(f"[Agent Error] {exc}")
        return

    # ==================================================
    # 8. Final Answer
    # ==================================================

    print()
    print("=" * 60)
    print("FINAL ANSWER")
    print("=" * 60)
    print(final_message.content)

    # ==================================================
    # 9. Host 独立验证：VerificationPipeline + 磁盘状态
    # ==================================================

    print()
    print("=" * 60)
    print("HOST VERIFICATION")
    print("=" * 60)

    # 磁盘状态断言。
    assert not (demo_root / "src" / "legacy_math.py").exists(), (
        "legacy_math.py 仍存在"
    )
    assert (demo_root / "src" / "math_utils.py").exists(), (
        "math_utils.py 不存在"
    )
    assert not (demo_root / "src" / "unused.py").exists(), (
        "unused.py 仍存在"
    )
    assert (
        "from src.math_utils import add"
        in (demo_root / "src" / "service.py").read_text()
    ), "service.py import 未更新"

    print("[PASS] 磁盘状态正确")

    verification_pipeline = VerificationPipeline(
        [
            pytest_verifier(
                process_runner,
                powershell_config(),
                command="uv run python -m pytest -q",
            )
        ]
    )

    summary = await verification_pipeline.run(
        workspace=workspace
    )

    print()
    print(f"[Summary] passed={summary.passed}")

    for result in summary.results:
        print(f"  [{result.name}] passed={result.passed} "
              f"exit_code={result.exit_code}")

    assert summary.passed is True, (
        "Agent 最终没有让 verification 全部通过\n"
        + "\n".join(
            f"{r.name}:\n{r.stdout}\n{r.stderr}"
            for r in summary.results
        )
    )

    print("[PASS] Verification pipeline passed")

    # ==================================================
    # 10. 全部完成
    # ==================================================

    print()
    print("=" * 60)
    print("ALL V5 E2E CHECKS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
