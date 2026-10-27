"""Formatter runtime：格式化 changed files，并经 mutation safety 落盘。

Formatter 与普通 verifier 的区别：它会修改文件。

因此不直接写 workspace，而是：
    Read snapshot
    → formatter(临时内容)
    → formatted content
    → 构造 ChangeSet
    → Patch Runtime
    → 真实 workspace mutation
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from coding_agent.changes.checkpoint import CheckpointStore
from coding_agent.changes.ir import ChangeSet, FileChange, TextReplacement
from coding_agent.editing.patch import PatchRuntime
from coding_agent.filesystem.mutation_queue import FileMutationQueue
from coding_agent.filesystem.snapshots import snapshot_file
from coding_agent.filesystem.workspace import Workspace
from coding_agent.verification.models import (
    FormatterResult,
    VerificationKind,
    VerificationResult,
)

# format_fn：输入内部 LF 文本，返回格式化后的 LF 文本。
FormatFn = Callable[
    [str],
    str | Awaitable[str],
]


class FormatterRuntime:
    """对 changed_files 运行 formatter，改动走 PatchRuntime。"""

    def __init__(
        self,
        workspace: Workspace,
        mutation_queue: FileMutationQueue,
        checkpoint_store: CheckpointStore,
        *,
        format_fn: FormatFn,
    ) -> None:
        self.workspace = workspace
        self.patch_runtime = PatchRuntime(
            workspace,
            mutation_queue,
            checkpoint_store,
        )
        self.format_fn = format_fn

    async def format(
        self,
        *,
        changed_files: list[str],
    ) -> FormatterResult:
        # --------------------------------------------------
        # 1. 逐个 snapshot + formatter，只收集有变化的文件。
        #    此阶段不触碰 workspace。
        # --------------------------------------------------

        file_changes: list[FileChange] = []
        changed_paths: list[str] = []

        for path in changed_files:
            file_path = self.workspace.resolve(path)

            if not file_path.exists():
                continue

            snapshot = snapshot_file(
                file_path,
                workspace_relative=path,
            )

            formatted = await self._apply_format(
                snapshot.content
            )

            if formatted == snapshot.content:
                continue

            file_changes.append(
                FileChange(
                    path=path,
                    expected_version=snapshot.version,
                    replacements=[
                        TextReplacement(
                            old_text=snapshot.content,
                            new_text=formatted,
                        )
                    ],
                )
            )

            changed_paths.append(path)

        # --------------------------------------------------
        # 2. 无变化 → changed=False。
        # --------------------------------------------------

        if not file_changes:
            return FormatterResult(
                changed=False,
                mutations=[],
                verification=VerificationResult(
                    kind=VerificationKind.FORMAT,
                    name="format",
                    passed=True,
                ),
            )

        # --------------------------------------------------
        # 3. 有变化 → 构造 ChangeSet → Patch Runtime。
        # --------------------------------------------------

        patch_result = await self.patch_runtime.apply(
            ChangeSet(files=file_changes)
        )

        return FormatterResult(
            changed=True,
            mutations=patch_result.mutations,
            verification=VerificationResult(
                kind=VerificationKind.FORMAT,
                name="format",
                passed=True,
                command=None,
                exit_code=0,
                diagnostics=[],
            ),
        )

    async def _apply_format(self, content: str) -> str:
        result = self.format_fn(content)

        if isinstance(result, Awaitable):
            return await result

        return result
