"""Agent runtime tool interfaces."""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from ai.tools import ToolDefinition


class AgentToolResult(BaseModel):
    """工具成功执行后返回给 Agent Runtime 的结果。

    content:
        主要结果文本，之后通常会进入 ToolResultMessage 发回模型。

    details:
        Runtime / 日志 / UI 使用的结构化附加信息，
        默认不会直接发送给模型。
    """

    content: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class AgentTool(ABC):
    """Agent Runtime 可执行工具的统一接口。"""

    name: str
    description: str
    parameters: dict[str, Any]
    capabilities: frozenset[str] = frozenset()
    @property
    def definition(self) -> ToolDefinition:
        """返回发送给模型 / Provider 的工具定义。"""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        """执行工具。

        执行成功：
            返回 AgentToolResult。

        执行失败：
            抛出异常，由 ToolExecutor 统一捕获并转换为错误 ToolResultMessage。
        """
        raise NotImplementedError
