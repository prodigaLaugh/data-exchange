from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class PushStateStore:
    """
    记录聚水潭推送已成功但飞书可能尚未回写的订单，用于幂等重试。
    键为 so_id（申请编号）。
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load_unlocked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    def get(self, so_id: str) -> dict[str, Any] | None:
        key = so_id.strip()
        if not key:
            return None
        with self._lock:
            entry = self._load_unlocked().get(key)
            return dict(entry) if isinstance(entry, dict) else None

    def is_jst_success(self, so_id: str) -> bool:
        entry = self.get(so_id)
        return bool(entry and entry.get("jst_success") is True)

    def mark_jst_success(
        self,
        so_id: str,
        *,
        record_id: str = "",
        o_id: str = "",
        msg: str = "",
    ) -> None:
        key = so_id.strip()
        if not key:
            return
        with self._lock:
            data = self._load_unlocked()
            data[key] = {
                "jst_success": True,
                "record_id": record_id,
                "o_id": o_id,
                "msg": msg,
                "jst_at": int(time.time()),
                "feishu_synced": False,
            }
            self._save_unlocked(data)

    def mark_feishu_synced(self, so_id: str) -> None:
        key = so_id.strip()
        if not key:
            return
        with self._lock:
            data = self._load_unlocked()
            if key not in data:
                return
            data[key]["feishu_synced"] = True
            data[key]["feishu_at"] = int(time.time())
            self._save_unlocked(data)
