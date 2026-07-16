from __future__ import annotations

import json
import threading
from pathlib import Path


def line_index_key(so_id: str, sku_id: str) -> str:
    return f"{so_id.strip()}|{sku_id.strip()}"


class OrderLineIndexStore:
    """so_id + sku_id → 飞书 record_id，用于电商订单明细 upsert。"""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def _load_unlocked(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return {str(k): str(v) for k, v in data.items() if k and v}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_unlocked(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    def get(self, so_id: str, sku_id: str) -> str | None:
        key = line_index_key(so_id, sku_id)
        if not key or key == "|":
            return None
        with self._lock:
            return self._load_unlocked().get(key)

    def set(self, so_id: str, sku_id: str, record_id: str) -> None:
        key = line_index_key(so_id, sku_id)
        rid = record_id.strip()
        if not key or key == "|" or not rid:
            return
        with self._lock:
            data = self._load_unlocked()
            data[key] = rid
            self._save_unlocked(data)

    def set_many(self, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        with self._lock:
            data = self._load_unlocked()
            data.update(mapping)
            self._save_unlocked(data)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._load_unlocked()
