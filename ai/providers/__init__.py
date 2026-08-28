"""Model provider implementations."""
from typing import Protocol

from ..context import Context
from ..messages import AssistantMessage
from ..models import Model


class Provider(Protocol):

    async def complete(
        self,
        model: Model,
        context: Context,
    ) -> AssistantMessage:
        ...
        