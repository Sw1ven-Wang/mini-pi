"""Agent tool registry."""

from __future__ import annotations

from collections.abc import Iterable

from ai.tools import ToolDefinition

from .tools import AgentTool


class ToolRegistry:
    """Agent 可用工具的注册表。

    Registry 只负责：

    - 注册 AgentTool
    - 保证工具名称唯一
    - 根据名称查找工具
    - 导出 ToolDefinition

    Registry 不负责真正执行工具。
    """

    def __init__(
        self,
        tools: Iterable[AgentTool] | None = None,
    ) -> None:
        self._tools: dict[str, AgentTool] = {}

        if tools is not None:
            for tool in tools:
                self.register(tool)

    def register(
        self,
        tool: AgentTool,
    ) -> None:
        """注册一个工具。

        Raises:
            ValueError:
                如果工具名称已经存在。
        """

        if tool.name in self._tools:
            raise ValueError(
                f"工具名称重复: {tool.name}"
            )

        self._tools[tool.name] = tool

    def get(
        self,
        name: str,
    ) -> AgentTool | None:
        """根据名称查找工具。

        不存在时返回 None。
        """

        return self._tools.get(name)

    @property
    def tools(self) -> list[AgentTool]:
        """返回当前所有已注册工具。"""

        return list(self._tools.values())

    @property
    def definitions(self) -> list[ToolDefinition]:
        """返回发送给模型的 ToolDefinition 列表。"""

        return [
            tool.definition
            for tool in self._tools.values()
        ]

    def __contains__(
        self,
        name: str,
    ) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)