from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class Debouncer:
    """按 key 防抖，防止同一 tableId 被频繁重复提交。"""

    def __init__(self, cooldown_seconds: int) -> None:
        self._cooldown = cooldown_seconds
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key, 0.0)
            if now - last < self._cooldown:
                return False
            self._last[key] = now
            return True

    def run(self, key: str, fn: Callable[[], T]) -> T | None:
        if not self.try_acquire(key):
            return None
        return fn()
