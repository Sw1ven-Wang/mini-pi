"""V5 E2E: move + patch + delete + verification."""

import asyncio
import os
from pathlib import Path

import pytest

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.versions import version_of_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.process.runner import ProcessRunner
from coding_agent.process.shell import powershell_config
from coding_agent.tools.move import MoveTool
from coding_agent.tools.patch import PatchTool
from coding_agent.tools.delete import DeleteTool
from coding_agent.verification.pipeline import VerificationPipeline
from coding_agent.verification.presets import pytest_verifier


def _run(coro):
    return asyncio.run(coro)


def _make_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()

    (root / "src" / "legacy_math.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8",
    )

    (root / "src" / "service.py").write_text(
        "from src.legacy_math import add\n"
        "\n"
        "\n"
        "def total(a, b):\n"
        "    return add(a, b)\n",
        encoding="utf-8",
    )

    (root / "src" / "unused.py").write_text(
        "# deprecated\n",
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
def test_v5_full_lifecycle_e2e(tmp_path: Path):
    _make_project(tmp_path)

    workspace = Workspace(tmp_path)
    mutation_queue = FileMutationQueue()
    checkpoint_store = CheckpointStore()

    # --------------------------------------------------
    # 1. move legacy_math.py → math_utils.py
    # --------------------------------------------------

    move_tool = MoveTool(
        workspace, mutation_queue, checkpoint_store
    )

    move_result = _run(
        move_tool.execute(
            "call_move",
            {
                "source": "src/legacy_math.py",
                "destination": "src/math_utils.py",
                "expected_source_version": version_of_file(
                    tmp_path / "src" / "legacy_math.py"
                ).model_dump(),
            },
        )
    )

    assert "src/math_utils.py" in move_result.content
    assert not (tmp_path / "src" / "legacy_math.py").exists()
    assert (tmp_path / "src" / "math_utils.py").exists()

    # --------------------------------------------------
    # 2. patch service.py import
    # --------------------------------------------------

    patch_tool = PatchTool(
        workspace, mutation_queue, checkpoint_store
    )

    patch_result = _run(
        patch_tool.execute(
            "call_patch",
            {
                "files": [
                    {
                        "path": "src/service.py",
                        "replacements": [
                            {
                                "old_text": (
                                    "from src.legacy_math "
                                    "import add"
                                ),
                                "new_text": (
                                    "from src.math_utils "
                                    "import add"
                                ),
                            }
                        ],
                    }
                ]
            },
        )
    )

    assert "Modified 1 file" in patch_result.content

    # --------------------------------------------------
    # 3. delete unused.py
    # --------------------------------------------------

    delete_tool = DeleteTool(
        workspace, mutation_queue, checkpoint_store
    )

    delete_result = _run(
        delete_tool.execute(
            "call_delete",
            {
                "path": "src/unused.py",
                "expected_version": version_of_file(
                    tmp_path / "src" / "unused.py"
                ).model_dump(),
            },
        )
    )

    assert "已删除" in delete_result.content
    assert not (tmp_path / "src" / "unused.py").exists()

    # --------------------------------------------------
    # 4. Host 独立 verification
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

    summary = _run(pipeline.run(workspace=workspace))

    assert summary.passed is True

    # Host 最终状态断言。
    assert not (tmp_path / "src" / "legacy_math.py").exists()
    assert (tmp_path / "src" / "math_utils.py").exists()
    assert not (tmp_path / "src" / "unused.py").exists()
    assert (
        "from src.math_utils import add"
        in (tmp_path / "src" / "service.py").read_text()
    )
