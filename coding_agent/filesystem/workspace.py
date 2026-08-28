"""Workspace path boundary and normalization."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from coding_agent.errors import PathOutsideWorkspaceError


class Workspace:
    """Coding Agent 可访问的本地工作区。

    Workspace 是所有文件系统工具的统一路径边界。

    模型传入的文件路径应当是 workspace-relative path，
    Workspace 负责：

    1. 保存规范化后的 workspace root。
    2. 将相对路径解析成绝对路径。
    3. 阻止路径逃逸 workspace。
    4. 为日志 / Tool Result 生成统一的相对 POSIX 路径。
    """

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

        if not self._root.exists():
            raise ValueError(
                f"Workspace root 不存在: {self._root}"
            )

        if not self._root.is_dir():
            raise ValueError(
                f"Workspace root 不是目录: {self._root}"
            )

    @property
    def root(self) -> Path:
        """返回规范化后的 workspace 根目录。"""
        return self._root

    def resolve(self, path: str | Path) -> Path:
        """将 workspace-relative path 转换为安全绝对路径。

        Args:
            path:
                模型或 Tool 提供的 workspace 相对路径。

        Returns:
            位于 workspace 内部的规范化绝对 Path。

        Raises:
            ValueError:
                path 为空、包含 NUL 或使用绝对路径。

            PathOutsideWorkspaceError:
                路径解析后位于 workspace 外部。
        """

        raw = str(path)

        if not raw:
            raise ValueError("path 不能为空")

        if "\x00" in raw:
            raise ValueError("path 不能包含 NUL 字符")

        input_path = Path(raw)

        # --------------------------------------------------
        # 模型侧路径统一要求使用 workspace-relative path。
        # --------------------------------------------------

        if input_path.is_absolute():
            raise ValueError(
                f"path 必须是 workspace 相对路径: {raw}"
            )

        # PureWindowsPath 可以额外识别：
        #
        # C:\\...
        # \\\\server\\share
        # \\\\?\\C:\\...
        #
        # 即使以后测试运行在非 Windows 环境，也能较早拒绝
        # Windows 风格绝对路径。
        windows_path = PureWindowsPath(raw)

        if windows_path.is_absolute() or windows_path.drive:
            raise ValueError(
                f"path 必须是 workspace 相对路径: {raw}"
            )

        # --------------------------------------------------
        # 组合 workspace root，然后 resolve。
        #
        # resolve(strict=False)：
        #
        # - 文件可以尚不存在，因此 WriteTool 也能使用。
        # - 已存在的 symlink 路径会被解析，有助于发现
        #   symlink 指向 workspace 外部的情况。
        # --------------------------------------------------

        candidate = (
            self._root / input_path
        ).resolve(strict=False)

        # --------------------------------------------------
        # 最关键的 containment check。
        #
        # 如果 candidate 无法 relative_to(root)，
        # 就说明它已经逃逸 workspace。
        # --------------------------------------------------

        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise PathOutsideWorkspaceError(
                raw
            ) from exc

        return candidate

    def relative(self, path: str | Path) -> str:
        """将 workspace 内的路径转换成统一 POSIX 相对路径。

        示例：

        Windows:
            E:\\repo\\src\\main.py

        返回：
            src/main.py
        """

        candidate = Path(path).resolve(strict=False)

        try:
            relative_path = candidate.relative_to(
                self._root
            )
        except ValueError as exc:
            raise PathOutsideWorkspaceError(
                str(path)
            ) from exc

        return relative_path.as_posix()