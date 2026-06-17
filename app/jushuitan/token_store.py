from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


class TokenStore:
    """持久化聚水潭 access_token / refresh_token。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self._path.exists():
                return {}
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取 token 文件失败: %s", e)
                return {}

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


class TokenManager:
    """过期前刷新 refresh_token；过期后 refresh 或 getInitToken，不中断业务。"""

    REFRESH_BEFORE_SECONDS = 3600

    def __init__(self, store: TokenStore) -> None:
        self._store = store
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """标记 token 已失效，下次调用将重新获取。"""
        with self._lock:
            cached = self._store.load()
            cached["expire_at"] = 0
            self._store.save(cached)
        logger.info("已标记聚水潭 access_token 失效，将在下次请求时重新获取")

    def get_valid_token(
        self,
        *,
        fetch_init: Callable[[], dict[str, Any]],
        fetch_refresh: Callable[[str], dict[str, Any]],
        force: bool = False,
    ) -> str:
        with self._lock:
            cached = self._store.load()
            now = int(time.time())
            token = cached.get("access_token")
            expire_at = int(cached.get("expire_at") or 0)
            refresh_token = cached.get("refresh_token")

            if not force and token and now < expire_at - self.REFRESH_BEFORE_SECONDS:
                return str(token)

            if not force and token and now < expire_at and refresh_token:
                logger.info("聚水潭 access_token 临近过期，提前 refresh")
                try:
                    data = fetch_refresh(str(refresh_token))
                    self._persist(data)
                    return str(data["access_token"])
                except Exception:
                    logger.exception("聚水潭 refresh_token 提前刷新失败，将尝试重新获取")

            if refresh_token:
                logger.info("聚水潭 access_token 已过期或失效，尝试 refresh_token")
                try:
                    data = fetch_refresh(str(refresh_token))
                    self._persist(data)
                    return str(data["access_token"])
                except Exception:
                    logger.exception("聚水潭 refresh_token 刷新失败，将重新 getInitToken")

            logger.info("聚水潭获取新的 init token")
            data = fetch_init()
            self._persist(data)
            return str(data["access_token"])

    def _persist(self, data: dict[str, Any]) -> None:
        expires_in = int(data.get("expires_in") or 2592000)
        cached = self._store.load()
        refresh_token = data.get("refresh_token") or cached.get("refresh_token")
        payload = {
            "access_token": data["access_token"],
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "expire_at": int(time.time()) + expires_in,
            "updated_at": int(time.time()),
        }
        self._store.save(payload)
        logger.info("聚水潭 access_token 已更新，有效期约 %s 秒", expires_in)
