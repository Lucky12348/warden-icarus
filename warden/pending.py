"""Selections awaiting confirmation, referenced by a short token in the custom_ids.

Discord custom_ids are limited to 100 characters: file names are not put there, only a
token. Tokens expire (15 min, like an interaction token) and are lost when the bot
restarts; the user then simply starts over.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable

TTL_SECONDS = 900.0


class Pending:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, int, dict[str, Any]]] = {}

    def put(self, user_id: int, **data: Any) -> str:
        self._purge()
        token = secrets.token_hex(5)
        self._items[token] = (self._clock(), user_id, data)
        return token

    def get(self, token: str, user_id: int) -> dict[str, Any] | None:
        """Token data if it exists, has not expired and belongs to this user."""
        self._purge()
        item = self._items.get(token)
        if item is None or item[1] != user_id:
            return None
        return item[2]

    def pop(self, token: str, user_id: int) -> dict[str, Any] | None:
        data = self.get(token, user_id)
        if data is not None:
            del self._items[token]
        return data

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, (at, _, _) in self._items.items() if now - at > TTL_SECONDS]:
            del self._items[token]
