"""V3 E2E: repository navigation + patch + verification.

在真实 tool runtime 上驱动完整链路（不依赖 LLM API key）：

    glob → search → read → patch → pytest → passed
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
from coding_agent.search.glob import GlobRuntime
from coding_agent.search.text_search import TextSearchRuntime
from coding_agent.tools.glob import GlobTool
from coding_agent.tools.patch import PatchTool
from coding_agent.tools.read import ReadTool
from coding_agent.tools.search import SearchTool


def _run(coro):
    return asyncio.run(coro)


def _make_demo_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()

    (root / "src" / "math_utils.py").write_text(
        "def old_add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    (root / "src" / "service.py").write_text(
        "from src.math_utils import old_add\n"
        "\n"
        "\n"
        "def total(a, b):\n"
        "    return old_add(a, b)\n",
        encoding="utf-8",
    )

    (root / "tests" / "test_service.py").write_text(
        "from src.service import total\n"
        "\n"
        "\n"
        "def test_total():\n"
        "    assert total(2, 3) == 5\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    os.name != "nt",
    reason="需要 Windows PowerShell 运行 pytest",
)
def test_v3_repository_navigation_e2e(tmp_path: Path):
    _make_demo_project(tmp_path)

    workspace = Workspace(tmp_path)

    glob_runtime = GlobRuntime(workspace)
    search_runtime = TextSearchRuntime(workspace)

    # --------------------------------------------------
    # 1. glob：定位所有 Python 文件
    # --------------------------------------------------

    glob_result = _run(glob_runtime.glob("**/*.py"))

    assert "src/math_utils.py" in {
        m.path for m in glob_result.matches
    }

    # --------------------------------------------------
    # 2. search：定位 old_add 的所有出现
    # --------------------------------------------------

    search_result = _run(
        search_runtime.search_text(
            "old_add",
            glob="**/*.py",
        )
    )

    paths = {m.path for m in search_result.matches}
    assert "src/math_utils.py" in paths
    assert "src/service.py" in paths

    # --------------------------------------------------
    # 3. read：读取需要修改的文件
    # --------------------------------------------------

    read_tool = ReadTool(workspace)

    math_snapshot = _run(
        read_tool.execute(
            "call_read_math",
            {"path": "src/math_utils.py"},
        )
    )

    service_snapshot = _run(
        read_tool.execute(
            "call_read_service",
            {"path": "src/service.py"},
        )
    )

    assert "old_add" in math_snapshot.content
    assert "old_add" in service_snapshot.content

    # --------------------------------------------------
    # 4. patch：两个文件一起重命名 old_add → add
    # --------------------------------------------------

    from coding_agent.changes.ir import (
        ChangeSet,
        FileChange,
        TextReplacement,
    )

    patch_tool = PatchTool(
        workspace,
        FileMutationQueue(),
        CheckpointStore(),
    )

    patch_result = _run(
        patch_tool.execute(
            "call_patch",
            {
                "files": [
                    {
                        "path": "src/math_utils.py",
                        "replacements": [
                            {
                                "old_text": "def old_add(a, b):",
                                "new_text": "def add(a, b):",
                            }
                        ],
                    },
                    {
                        "path": "src/service.py",
                        "replacements": [
                            {
                                "old_text": (
                                    "from src.math_utils "
                                    "import old_add"
                                ),
                                "new_text": (
                                    "from src.math_utils "
                                    "import add"
                                ),
                            },
                            {
                                "old_text": "return old_add(a, b)",
                                "new_text": "return add(a, b)",
                            },
                        ],
                    },
                ]
            },
        )
    )

    assert "Modified 2 files" in patch_result.content

    # 磁盘已更新。
    assert "old_add" not in (
        tmp_path / "src" / "math_utils.py"
    ).read_text()
    assert "old_add" not in (
        tmp_path / "src" / "service.py"
    ).read_text()

    # --------------------------------------------------
    # 5. powershell pytest：验证修改
    # --------------------------------------------------

    runner = ProcessRunner()

    verification = _run(
        runner.run(
            "uv run python -m pytest -q",
            cwd=tmp_path,
            shell_config=powershell_config(),
            timeout_seconds=60,
        )
    )

    assert verification.exit_code == 0, (
        f"pytest 失败:\n"
        f"stdout:\n{verification.stdout}\n"
        f"stderr:\n{verification.stderr}"
    )
