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
_log_dir_override: Path | None = None


def set_log_dir(path: str | Path) -> None:
    """启动时设置日志目录（绝对路径），避免相对路径因工作目录不同写错位置。"""
    global _log_dir_override
    _log_dir_override = Path(path)


def _resolve_log_dir() -> Path:
    if _log_dir_override is not None:
        return _log_dir_override
    try:
        from app.config import get_settings

        return Path(get_settings().log_dir)
    except Exception:
        return Path(_DEFAULT_LOG_DIR)


def _write_json_line(log_file: Path, entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False, default=str)
    log_dir = _resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def log_api_error(
    *,
    source: str,
    message: str,
    request_snapshot: dict[str, Any] | None = None,
    response_status: int | None = None,
    response_body: Any = None,
    code: Any = None,
    path: str | None = None,
    context: dict[str, Any] | None = None,
    exc: BaseException | None = None,
    trace_file: str | None = None,
    request_id: str | None = None,
) -> None:
    """
    统一 API 错误日志：包含请求地址、参数、响应。
    写入 logs/YYYY-MM-DD.log；若指定 trace_file 则同时写入专用 trace 日志。
    """
    entry: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event": "request_failed",
        "source": source,
        "message": message,
    }
    if request_id:
        entry["request_id"] = request_id
    if code is not None:
        entry["code"] = code
    if path:
        entry["request_path"] = path
    if request_snapshot:
        entry.update(request_snapshot)
    if response_status is not None:
        entry["response_status"] = response_status
    if response_body is not None:
        entry["response_body"] = response_body
    if context:
        entry["context"] = context
    if exc is not None:
        entry["exception"] = exc.__class__.__name__
        entry["traceback"] = traceback.format_exc()

    try:
        daily = _resolve_log_dir() / f"{datetime.now():%Y-%m-%d}.log"
        _write_json_line(daily, entry)
        if trace_file:
            trace = _resolve_log_dir() / f"{trace_file}-{datetime.now():%Y-%m-%d}.log"
            _write_json_line(trace, entry)
    except OSError as e:
        logger.error("写入 API 错误日志失败: %s entry=%s", e, entry)


def log_failure(
    source: str,
    message: str,
    *,
    code: Any = None,
    path: str | None = None,
    context: dict[str, Any] | None = None,
    exc: BaseException | None = None,
    request_snapshot: dict[str, Any] | None = None,
    response_status: int | None = None,
    response_body: Any = None,
) -> None:
    """兼容旧调用；新代码请优先使用 log_api_error。"""
    log_api_error(
        source=source,
        message=message,
        code=code,
        path=path,
        context=context,
        exc=exc,
        request_snapshot=request_snapshot,
        response_status=response_status,
        response_body=response_body,
    )


def log_batch_push_trace(request_id: str, entry: dict[str, Any]) -> None:
    """将批量推送全过程 trace 追加写入 logs/batch-push-YYYY-MM-DD.log。"""
    record: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        **entry,
    }
    try:
        log_file = _resolve_log_dir() / f"batch-push-{datetime.now():%Y-%m-%d}.log"
        _write_json_line(log_file, record)
    except OSError as e:
        logger.error("写入 batch-push trace 失败: %s entry=%s", e, record)


def log_single_push_trace(request_id: str, entry: dict[str, Any]) -> None:
    """将单行推送全过程 trace 追加写入 logs/single-push-YYYY-MM-DD.log。"""
    record: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        **entry,
    }
    try:
        log_file = _resolve_log_dir() / f"single-push-{datetime.now():%Y-%m-%d}.log"
        _write_json_line(log_file, record)
    except OSError as e:
        logger.error("写入 single-push trace 失败: %s entry=%s", e, record)


def log_logistics_trace(request_id: str, entry: dict[str, Any]) -> None:
    """将物流同步全过程 trace 追加写入 logs/logistics-YYYY-MM-DD.log。"""
    record: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
        **entry,
    }
    try:
        log_file = _resolve_log_dir() / f"logistics-{datetime.now():%Y-%m-%d}.log"
        _write_json_line(log_file, record)
    except OSError as e:
        logger.error("写入 logistics trace 失败: %s entry=%s", e, record)
