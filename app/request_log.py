from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.failure_log import build_error_log_entry, log_api_error

logger = logging.getLogger(__name__)

_RECORD_ID_KEYS = ("recordId", "record_id", "record id", "记录ID", "记录 id")


def _parse_request_body(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        text = raw.decode("utf-8", errors="replace")
        return text if len(text) <= 2000 else text[:2000] + "..."


def resolve_record_id(
    request: Request,
    *,
    query_record_id: str | None = None,
    query_record_id_snake: str | None = None,
) -> str:
    """
    从 query、JSON body、form、纯文本 body 解析 record_id。
    兼容飞书工作流 HTTP 节点多种传参方式。
    """
    for val in (query_record_id, query_record_id_snake):
        if val and str(val).strip():
            return str(val).strip()

    raw: bytes = getattr(request.state, "raw_body", b"")
    if not raw:
        return ""

    content_type = (request.headers.get("content-type") or "").lower()
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""

    if "application/x-www-form-urlencoded" in content_type:
        form = parse_qs(text, keep_blank_values=True)
        for key in _RECORD_ID_KEYS:
            vals = form.get(key)
            if vals and str(vals[0]).strip():
                return str(vals[0]).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 飞书偶发发送非 JSON 纯文本 record_id
        if text and not text.startswith("{"):
            return text.strip()
        return ""

    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in _RECORD_ID_KEYS:
            val = data.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def build_error_log_from_request(request: Request, response: Any) -> dict[str, Any]:
    raw_body = getattr(request.state, "raw_body", b"")
    return build_error_log_entry(
        request_url=str(request.url),
        request_method=request.method,
        query=dict(request.query_params),
        body=_parse_request_body(raw_body),
        response=response,
    )


def is_failure_logged(request: Request) -> bool:
    return bool(getattr(request.state, "failure_logged", False))


def mark_failure_logged(request: Request) -> None:
    request.state.failure_logged = True


def log_request_failure(
    request: Request,
    response_body: dict[str, Any],
    *,
    trace_file: str | None = None,
) -> None:
    """仅当响应不成功时写入 logs/（含 single-push-*.log）。"""
    if response_body.get("ok") is True:
        return
    if is_failure_logged(request):
        return

    log_payload: dict[str, Any] = dict(response_body)
    detail = response_body.get("detail")
    if isinstance(detail, dict):
        log_payload = {
            "ok": response_body.get("ok"),
            "request_id": response_body.get("request_id"),
            "message": response_body.get("message"),
            "debounced": response_body.get("debounced"),
            "errors": detail.get("errors") or response_body.get("errors"),
            "steps": detail.get("steps"),
            "jst_response": detail.get("jst_response"),
            "record_id": detail.get("record_id"),
            "so_id": detail.get("so_id"),
            "detail": detail,
        }
    elif "detail" in response_body and "ok" not in response_body:
        log_payload = {"ok": False, **response_body}

    log_api_error(
        build_error_log_from_request(request, log_payload),
        trace_file=trace_file,
    )
    mark_failure_logged(request)
    logger.error("API失败已记录 request_path=%s payload=%s", request.url.path, log_payload)


class RequestBodyMiddleware(BaseHTTPMiddleware):
    """缓存请求体，供异常处理与错误日志使用。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw_body = await request.body()
        request.state.raw_body = raw_body
        return await call_next(request)


def _trace_file_for_path(path: str) -> str | None:
    if path == "/api/v1/push-jushuitan":
        return "single-push"
    if path == "/api/v1/orders/sync":
        return "order-sync"
    if path == "/api/v1/batch-push":
        return "batch-push"
    return None


def register_exception_handlers(app) -> None:
    from fastapi import HTTPException

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            content: dict[str, Any] = {"detail": exc.detail}
        else:
            content = {"detail": exc.detail}
        if not is_failure_logged(request):
            log_api_error(
                build_error_log_from_request(request, content),
                trace_file=_trace_file_for_path(request.url.path),
            )
            mark_failure_logged(request)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        content = {
            "detail": exc.errors(),
            "hint": (
                "请求参数无效。飞书工作流请使用 JSON："
                '{"recordId":"{{记录ID}}"}，或 URL 查询参数 ?recordId={{记录ID}}'
            ),
        }
        log_api_error(
            build_error_log_from_request(request, content),
            trace_file=_trace_file_for_path(request.url.path),
        )
        return JSONResponse(status_code=422, content=content)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 path=%s", request.url.path)
        content = {"detail": {"error": str(exc)}}
        log_api_error(build_error_log_from_request(request, content))
        return JSONResponse(status_code=500, content=content)
