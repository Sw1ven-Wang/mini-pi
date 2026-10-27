"""Ephemeral, bounded mutation preview storage."""

from __future__ import annotations

from collections import OrderedDict

from coding_agent.mutation.preview import MutationPreview, MutationPreviewObserver


class InMemoryPreviewStore(MutationPreviewObserver):
    def __init__(self, *, max_previews: int = 8) -> None:
        if max_previews < 1:
            raise ValueError("max_previews must be positive")
        self._max_previews = max_previews
        self._items: OrderedDict[str, MutationPreview] = OrderedDict()
        self._latest_id: str | None = None

    async def publish(self, preview: MutationPreview) -> None:
        self.publish_sync(preview)

    def publish_sync(self, preview: MutationPreview) -> None:
        copy = preview.model_copy(deep=True)
        self._items[copy.preview_id] = copy
        self._items.move_to_end(copy.preview_id)
        self._latest_id = copy.preview_id
        while len(self._items) > self._max_previews:
            self._items.popitem(last=False)

    def get(self, preview_id: str | None = None) -> MutationPreview | None:
        key = preview_id or self._latest_id
        if key is None:
            return None
        item = self._items.get(key)
        return item.model_copy(deep=True) if item else None

    def clear(self) -> None:
        self._items.clear()
        self._latest_id = None
