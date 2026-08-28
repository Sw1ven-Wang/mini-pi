
from collections.abc import AsyncIterator
from typing import Protocol

from ..context import Context
from ..events import AIEvent
from ..models import Model
from ..tools import ToolDefinition


class Provider(Protocol):

    def stream(
        self,
        model: Model,
        context: Context,
        tools: list[ToolDefinition],
    ) -> AsyncIterator[AIEvent]:
        ...