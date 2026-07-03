from __future__ import annotations

import json
import logging
import os
import threading
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


def _sanitize_log_body(body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    safe = dict(body)
    for key in ("sign", "access_token", "app_secret", "refresh_token", "app_key"):
        if key in safe:
            safe[key] = "***"
    return safe


def build_error_log_entry(
    *,
    request_url: str,
    request_method: str,
    response: Any,
    query: dict[str, Any] | None = None,
    body: Any = None,
    request_time: str | None = None,
) -> dict[str, Any]:
    """错误日志结构：请求时间、请求url、请求方法、请求参数、响应结果。"""
    return {
        "请求时间": request_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "请求url": request_url,
        "请求方法": request_method,
        "请求参数": {
            "query": dict(query or {}),
            "body": _sanitize_log_body(body) if body is not None else {},
        },
        "响应结果": response,
    }


def _log_dir_permission_hint(log_dir: Path, error: OSError) -> None:
    try:
        st = os.stat(log_dir)
        owner_uid, owner_gid = st.st_uid, st.st_gid
    except OSError:
        owner_uid, owner_gid = None, None
    logger.error(
        "日志目录不可写 path=%s owner_uid=%s owner_gid=%s current_uid=%s error=%s；"
        "请执行: sudo chown -R duijie:duijie %s && sudo chmod 775 %s",
        log_dir,
        owner_uid,
        owner_gid,
        os.getuid(),
        error,
        log_dir,
        log_dir,
    )


def verify_log_dir_writable() -> None:
    """启动时探测日志目录是否可写，不可写时输出到 journalctl。"""
    log_dir = _resolve_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        _log_dir_permission_hint(log_dir, e)


def log_api_error(
    entry: dict[str, Any],
    *,
    trace_file: str | None = None,
) -> None:
    """写入 logs/YYYY-MM-DD.log；可选同时写入专用 trace 日志。"""
    try:
        daily = _resolve_log_dir() / f"{datetime.now():%Y-%m-%d}.log"
        _write_json_line(daily, entry)
        if trace_file:
            trace = _resolve_log_dir() / f"{trace_file}-{datetime.now():%Y-%m-%d}.log"
            _write_json_line(trace, entry)
    except OSError as e:
        log_dir = _resolve_log_dir()
        _log_dir_permission_hint(log_dir, e)
        logger.error("写入 API 错误日志失败（详情见上条权限提示） entry_keys=%s", list(entry))


def log_failure(
    *,
    request_url: str,
    request_method: str,
    response: Any,
    query: dict[str, Any] | None = None,
    body: Any = None,
    trace_file: str | None = None,
) -> None:
    """记录上游/定时任务等错误，结构与 API 错误日志一致。"""
    log_api_error(
        build_error_log_entry(
            request_url=request_url,
            request_method=request_method,
            query=query,
            body=body,
            response=response,
        ),
        trace_file=trace_file,
    )
