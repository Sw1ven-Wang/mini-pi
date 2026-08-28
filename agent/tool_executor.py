from ai.messages import (
    ToolCall,
    ToolResultMessage,
)

from .tool_registry import ToolRegistry
from .tool_dispatcher import ToolDispatcher

class ToolExecutor:

    def __init__(
        self,
        dispatcher: ToolDispatcher,
    ):
        self.dispatcher = dispatcher


    async def execute(
        self,
        tool_call,
    ):
        return await self.dispatcher.dispatch(
            tool_call
        )

