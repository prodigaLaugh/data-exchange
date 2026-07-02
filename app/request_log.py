from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.failure_log import log_api_error

logger = logging.getLogger(__name__)

_SENSITIVE_HEADERS = frozenset({"authorization", "x-submit-api-key", "cookie"})


def _parse_request_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", errors="replace")
        return text if len(text) <= 2000 else text[:2000] + "..."


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADERS:
            out[key] = "***"
        else:
            out[key] = value
    return out


def build_request_snapshot(request: Request) -> dict[str, Any]:
    raw_body = getattr(request.state, "raw_body", b"")
    return {
        "request_url": str(request.url),
        "request_method": request.method,
        "request_path": request.url.path,
        "request_query": dict(request.query_params),
        "request_params": _parse_request_body(raw_body),
        "request_headers": _sanitize_headers(dict(request.headers)),
    }


class RequestBodyMiddleware(BaseHTTPMiddleware):
    """缓存请求体，供异常处理与错误日志使用。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw_body = await request.body()
        request.state.raw_body = raw_body
        return await call_next(request)


def _trace_file_for_path(path: str) -> str | None:
    if path == "/api/v1/push-jushuitan":
        return "single-push"
    if path == "/api/v1/batch-push":
        return "batch-push"
    return None


def register_exception_handlers(app) -> None:
    from fastapi import HTTPException

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        response_body: Any
        if isinstance(exc.detail, dict):
            response_body = {"detail": exc.detail}
        else:
            response_body = {"detail": exc.detail}
        content = response_body
        request_id = None
        if isinstance(exc.detail, dict):
            request_id = exc.detail.get("request_id")
        log_api_error(
            source="api",
            message=str(exc.detail),
            request_snapshot=build_request_snapshot(request),
            response_status=exc.status_code,
            response_body=content,
            trace_file=_trace_file_for_path(request.url.path),
            request_id=str(request_id) if request_id else None,
        )
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 path=%s", request.url.path)
        content = {"detail": {"error": str(exc)}}
        log_api_error(
            source="api",
            message=str(exc),
            request_snapshot=build_request_snapshot(request),
            response_status=500,
            response_body=content,
            exc=exc,
        )
        return JSONResponse(status_code=500, content=content)
