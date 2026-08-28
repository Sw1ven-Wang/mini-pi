"""Mini-Pi Bash E2E demo.

验证完整的 Coding Agent 闭环：

1. Bash 运行 pytest 并观察失败
2. ReadTool 读取目标文件
3. EditTool 修复 add 函数
4. Bash 再次运行 pytest 并确认通过
5. Host 独立验证最终文件、BOM、CRLF 和 pytest 结果
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
from coding_agent.safety.approvals import CLIApprovalHandler
from coding_agent.safety.permissions import CodingPermissionEngine
from coding_agent.tools.bash import BashTool
from coding_agent.tools.edit import EditTool
from coding_agent.tools.read import ReadTool


async def main() -> None:
    # ==================================================
    # 1. 创建独立 Demo Workspace 和失败测试 fixture
    # ==================================================

    project_root = Path.cwd()
    demo_root = project_root / ".mini_pi_demo"
    demo_root.mkdir(exist_ok=True)

    print(f"[Demo Workspace] {demo_root}")

    math_file = demo_root / "test1_2.py"

    # 每次运行都恢复固定的失败状态。
    # fixture 故意使用 UTF-8 BOM + CRLF，验证 EditTool 保持格式。
    math_file.write_bytes(
        b"\xef\xbb\xbf"
        b"def add(a, b):\r\n"
        b"    return a - b\r\n"
        b"\r\n"
        b"\r\n"
        b"def test_add():\r\n"
        b"    assert add(2, 3) == 5\r\n"
    )

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
    permission_engine = CodingPermissionEngine()
    approval_handler = CLIApprovalHandler()

    # ==================================================
    # 4. Tools
    # ==================================================

    read_tool = ReadTool(
        workspace=workspace,
    )

    edit_tool = EditTool(
        workspace=workspace,
        mutation_queue=mutation_queue,
        checkpoint_store=checkpoint_store,
    )

    bash_tool = BashTool(
        workspace=workspace,
        process_runner=process_runner,
    )

    # ==================================================
    # 5. Registry 和 Dispatcher
    # ==================================================

    registry = ToolRegistry(
        [
            read_tool,
            edit_tool,
            bash_tool,
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
            "处理真实文件时必须使用提供的工具，不能猜测。"
            "修改已存在的文件时，必须先 read，再使用 edit。"
            "必须通过 bash 实际运行测试来判断修复是否成功。"
        ),
        messages=[
            UserMessage(
                content=(
                    "请修复 test1_2.py 中导致测试失败的问题。\n"
                    "要求：\n"
                    "1. 先使用 bash 执行："
                    "uv run pytest test1_2.py -q\n"
                    "2. 如果测试失败，读取 test1_2.py。\n"
                    "3. 使用 edit 修复 add 函数，"
                    "使其正确返回两个参数之和。\n"
                    "4. 修改后再次使用 bash 执行："
                    "uv run pytest test1_2.py -q\n"
                    "5. 只有确认测试通过后，才能报告任务完成。"
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
            max_turns=15,
        )
    except AgentLoopError as exc:
        print(f"[Agent Error] {exc}")
        return

    # ==================================================
    # 8. Final Answer 和 Message History
    # ==================================================

    print()
    print("=" * 60)
    print("FINAL ANSWER")
    print("=" * 60)
    print(final_message.content)

    print()
    print("=" * 60)
    print("MESSAGE HISTORY")
    print("=" * 60)

    for index, message in enumerate(context.messages):
        print()
        print(f"[{index}] {type(message).__name__}")
        print(message)

    # ==================================================
    # 9. Host 独立验证磁盘结果
    # ==================================================

    print()
    print("=" * 60)
    print("DISK VERIFICATION")
    print("=" * 60)
    print(f"[Target File] {math_file.resolve()}")

    math_bytes = math_file.read_bytes()
    text = (
        math_bytes
        .decode("utf-8-sig")
        .replace("\r\n", "\n")
    )

    print()
    print("[Final File Content]")
    print(text)

    assert "return a + b" in text, (
        "add 函数没有改为加法"
    )
    assert "return a - b" not in text, (
        "add 函数仍然包含错误的减法实现"
    )
    assert "assert add(2, 3) == 5" in text, (
        "测试断言被意外修改"
    )

    print("[PASS] add 函数修复成功")

    bom = b"\xef\xbb\xbf"
    assert math_bytes.startswith(bom), (
        "EditTool 修改后 UTF-8 BOM 丢失"
    )

    print("[PASS] UTF-8 BOM 保持")

    payload = math_bytes[len(bom):]
    assert b"\r\n" in payload, (
        "EditTool 修改后 CRLF 丢失"
    )
    assert b"\n" not in payload.replace(b"\r\n", b""), (
        "文件中出现非 CRLF 的 LF"
    )

    print("[PASS] CRLF 保持")

    verification = await process_runner.run(
        "uv run pytest test1_2.py -q",
        cwd=demo_root,
        timeout_seconds=30,
    )

    print()
    print("[Host Verification]")

    if verification.stdout:
        print(verification.stdout)

    if verification.stderr:
        print(verification.stderr)

    assert verification.exit_code == 0, (
        "Agent 最终没有真正修复测试\n"
        f"stdout:\n{verification.stdout}\n"
        f"stderr:\n{verification.stderr}"
    )

    print("[PASS] Final pytest passed")

    # ==================================================
    # 10. 全部完成
    # ==================================================

    print()
    print("=" * 60)
    print("ALL BASH E2E CHECKS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
