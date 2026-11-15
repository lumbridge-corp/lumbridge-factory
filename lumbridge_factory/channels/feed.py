"""§4.4: what has no forge object to be posted on, like a question about main's CI, goes to the feed.
It lives in the store, and `lumbridge-factory status` and the snapshot show it."""

from __future__ import annotations

from typing import Any, Callable

from ..store import Store
from .gitea import thread_target


class RoutedChannel:
    """Issue and PR Threads go to the forge. Anything else (main's CI, office:, chat:) goes to the feed."""

    def __init__(self, forge: Any, store: Store, clock: Callable[[], float]):
        self.forge = forge
        self.store = store
        self.clock = clock

    def find_comment(self, thread: str, key: str) -> bool:
        return self.forge.find_comment(thread, key) if thread_target(thread) else self.store.has_feed(key)

    def post_comment(self, thread: str, text: str, key: str) -> None:
        if thread_target(thread):
            self.forge.post_comment(thread, text, key)
            return
        with self.store.tx():
            self.store.add_feed(key, thread, text, self.clock())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.forge, name)
