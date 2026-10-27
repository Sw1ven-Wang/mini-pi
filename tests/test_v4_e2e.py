"""V4 E2E: patch → verify → diagnose → fix → verify again.

真实 tool runtime 驱动（不依赖 LLM key）：
    glob/search/read → patch → formatter → pytest → diagnostics → fix → pass
"""

import asyncio
import os
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import powershell_config
from coding_agent.tools.patch import PatchTool
from coding_agent.verification.formatter import FormatterRuntime
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.presets import pytest_verifier


def _run(coro):
    return asyncio.run(coro)


def _make_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()

    # 同时存在：逻辑错误 + 格式问题。
    (root / "src" / "calc.py").write_text(
        "def add(a, b):\n"
        "return a - b\n",  # 逻辑错误 + 缺缩进
        encoding="utf-8",
    )

    (root / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    os.name != "nt",
    reason="需要 Windows PowerShell 运行 pytest",
)
def test_v4_verification_pipeline_e2e(tmp_path: Path):
    _make_project(tmp_path)

    workspace = Workspace(tmp_path)
    mutation_queue = FileMutationQueue()
    checkpoint_store = CheckpointStore()

    # --------------------------------------------------
    # 1. patch 修复逻辑错误
    # --------------------------------------------------

    patch_tool = PatchTool(
        workspace,
        mutation_queue,
        checkpoint_store,
    )

    patch_result = _run(
        patch_tool.execute(
            "call_patch",
            {
                "files": [
                    {
                        "path": "src/calc.py",
                        "replacements": [
                            {
                                "old_text": "return a - b",
                                "new_text": "return a + b",
                            }
                        ],
                    }
                ]
            },
        )
    )

    assert "Modified 1 file" in patch_result.content

    # --------------------------------------------------
    # 2. formatter 修复格式问题（走 mutation safety）
    # --------------------------------------------------

    formatter = FormatterRuntime(
        workspace,
        mutation_queue,
        checkpoint_store,
        format_fn=lambda content: content.replace(
            "\nreturn ",
            "\n    return ",
        ),
    )

    format_result = _run(
        formatter.format(changed_files=["src/calc.py"])
    )

    assert format_result.changed is True

    formatted = (tmp_path / "src" / "calc.py").read_text()
    assert formatted == (
        "def add(a, b):\n"
        "    return a + b\n"
    )

    # --------------------------------------------------
    # 3. VerificationPipeline 运行 pytest
    # --------------------------------------------------

    runner = ProcessRunner()

    pipeline = VerificationPipeline(
        [
            pytest_verifier(
                runner,
                powershell_config(),
                command="uv run python -m pytest -q",
            )
        ]
    )

    summary = _run(
        pipeline.run(workspace=workspace)
    )

    assert summary.passed is True, (
        f"verification failed:\n"
        + "\n".join(
            f"{r.name}: {r.stdout}\n{r.stderr}"
            for r in summary.results
        )
    )


@pytest.mark.skipif(
    os.name != "nt",
    reason="需要 Windows PowerShell 运行 pytest",
)
def test_v4_diagnose_and_fix_loop(tmp_path: Path):
    """失败 → 结构化诊断 → 修复 → 再验证。"""
    _make_project(tmp_path)

    workspace = Workspace(tmp_path)
    runner = ProcessRunner()

    pipeline = VerificationPipeline(
        [
            pytest_verifier(
                runner,
                powershell_config(),
                command="uv run python -m pytest -q",
            )
        ]
    )

    # 初始：逻辑错误 + 格式错误，pytest 失败。
    summary = _run(pipeline.run(workspace=workspace))

    assert summary.passed is False

    # 诊断：应包含失败文件。
    diag_paths = {
        d.path for d in summary.diagnostics if d.path
    }

    assert "src/calc.py" in diag_paths or any(
        p and p.endswith("test_calc.py")
        for p in diag_paths
    )

    # 修复 + 格式化。
    patch_tool = PatchTool(
        workspace,
        FileMutationQueue(),
        CheckpointStore(),
    )

    _run(
        patch_tool.execute(
            "call_fix",
            {
                "files": [
                    {
                        "path": "src/calc.py",
                        "replacements": [
                            {
                                "old_text": "def add(a, b):\nreturn a - b",
                                "new_text": (
                                    "def add(a, b):\n"
                                    "    return a + b"
                                ),
                            }
                        ],
                    }
                ]
            },
        )
    )

    # 再验证 → 通过。
    summary = _run(pipeline.run(workspace=workspace))

    assert summary.passed is True
