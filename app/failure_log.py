from __future__ import annotations

import json
import logging
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_DEFAULT_LOG_DIR = "logs"


def _resolve_log_dir() -> Path:
    try:
        from app.config import get_settings

        return Path(get_settings().log_dir)
    except Exception:
        return Path(_DEFAULT_LOG_DIR)


def log_failure(
    source: str,
    message: str,
    *,
    code: Any = None,
    path: str | None = None,
    context: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> None:
    """
    将接口/业务失败信息追加写入 logs/YYYY-MM-DD.log。
    """
    entry: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "message": message,
    }
    if code is not None:
        entry["code"] = code
    if path:
        entry["path"] = path
    if context:
        entry["context"] = context
    if exc is not None:
        entry["exception"] = exc.__class__.__name__
        entry["traceback"] = traceback.format_exc()

    line = json.dumps(entry, ensure_ascii=False, default=str)

    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{datetime.now():%Y-%m-%d}.log"
        with _lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        logger.error("写入失败日志文件失败: %s entry=%s", e, line)


def log_batch_push_trace(request_id: str, entry: dict[str, Any]) -> None:
    """将批量推送全过程 trace 追加写入 logs/batch-push-YYYY-MM-DD.log。"""
    record: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        **entry,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"batch-push-{datetime.now():%Y-%m-%d}.log"
        with _lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        logger.error("写入 batch-push trace 失败: %s entry=%s", e, line)


def log_single_push_trace(request_id: str, entry: dict[str, Any]) -> None:
    """将单行推送全过程 trace 追加写入 logs/single-push-YYYY-MM-DD.log。"""
    record: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        **entry,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"single-push-{datetime.now():%Y-%m-%d}.log"
        with _lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        logger.error("写入 single-push trace 失败: %s entry=%s", e, line)


def log_logistics_trace(request_id: str, entry: dict[str, Any]) -> None:
    """将物流同步全过程 trace 追加写入 logs/logistics-YYYY-MM-DD.log。"""
    record: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        **entry,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"logistics-{datetime.now():%Y-%m-%d}.log"
        with _lock:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        logger.error("写入 logistics trace 失败: %s entry=%s", e, line)
