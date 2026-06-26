from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


class Debouncer:
    """
    按 key 互斥：同一 tableId 须等上次请求处理完毕（完全响应）后才可再次进入。
    """

    def __init__(self, cooldown_seconds: int = 0) -> None:
        # cooldown_seconds 保留兼容配置，当前仅使用处理中互斥
        self._cooldown = cooldown_seconds
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def try_begin(self, key: str) -> bool:
        """尝试开始处理；若该 key 仍在处理中则返回 False。"""
        with self._lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    def end(self, key: str) -> None:
        """请求处理结束（成功或失败）后释放。"""
        with self._lock:
            self._active.discard(key)

    def try_acquire(self, key: str) -> bool:
        """兼容旧调用，等同 try_begin。"""
        return self.try_begin(key)

    def release(self, key: str) -> None:
        """兼容旧调用，等同 end。"""
        self.end(key)

    def run(self, key: str, fn: Callable[[], T]) -> T | None:
        if not self.try_begin(key):
            return None
        try:
            return fn()
        finally:
            self.end(key)
