from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import AliasChoices, BaseModel, Field

from app.config import Settings, get_settings
from app.debounce import Debouncer
from app.failure_log import set_log_dir, verify_log_dir_writable
from app.feishu.client import FeishuClient
from app.jushuitan.client import JushuitanClient
from app.request_log import (
    RequestBodyMiddleware,
    log_request_failure,
    register_exception_handlers,
    resolve_record_id,
)
from app.scheduler import shutdown_scheduler, start_scheduler
from app.services.batch_push import BatchPushResult, run_batch_push
from app.services.order_sync import OrderSyncResult, run_order_sync, year_range
from app.services.single_push import SinglePushResult, run_single_push
from datetime import datetime

from app.order_line_index import OrderLineIndexStore
from app.push_state import PushStateStore

logger = logging.getLogger(__name__)
_debouncer: Debouncer | None = None


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    set_log_dir(settings.log_dir)
    setup_logging(settings.log_level)
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    verify_log_dir_writable()
    global _debouncer
    _debouncer = Debouncer(settings.debounce_seconds)
    start_scheduler()
    logger.info("服务已启动，日志目录: %s", settings.log_dir)
    yield
    shutdown_scheduler()
    logger.info("服务已停止")


app = FastAPI(
    title="飞书多维表 ↔ 聚水潭对接",
    version="1.0.0",
    lifespan=lifespan,
)
register_exception_handlers(app)
app.add_middleware(RequestBodyMiddleware)


class BatchPushRequest(BaseModel):
    tableId: str = Field(..., description="飞书多维表格 table_id")


class SinglePushRequest(BaseModel):
    """OpenAPI 文档用；实际接口也支持 query / form 传 recordId。"""
    record_id: str = Field(
        ...,
        validation_alias=AliasChoices("recordId", "record_id"),
        description="飞书父表 record_id",
    )


class BatchPushResponse(BaseModel):
    ok: bool
    request_id: str
    debounced: bool = False
    message: str
    detail: dict[str, Any] | None = None


class OrderSyncResponse(BaseModel):
    ok: bool
    request_id: str
    message: str
    detail: dict[str, Any] | None = None


class OrderSyncRequest(BaseModel):
    """手动同步聚水潭订单明细到飞书电商表。"""
    begin: str | None = Field(
        None,
        description="modified_begin，如 2026-01-01 00:00:00",
    )
    end: str | None = Field(
        None,
        description="modified_end，如 2026-12-31 23:59:59",
    )
    year: int | None = Field(
        None,
        description="同步指定年份（与 begin/end 二选一，未传则默认今年）",
    )
    tableId: str | None = Field(None, description="飞书 table_id，默认 FEISHU_TABLE_DIANSHANG")


class SinglePushResponse(BaseModel):
    ok: bool
    request_id: str
    debounced: bool = False
    message: str
    detail: dict[str, Any] | None = None


def verify_submit_key(
    x_submit_api_key: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    if not x_submit_api_key or x_submit_api_key != settings.submit_api_key:
        logger.warning("拒绝未授权请求")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def _clients(settings: Settings) -> tuple[FeishuClient, JushuitanClient]:
    feishu = FeishuClient(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        settings.feishu_app_token,
    )
    jst = JushuitanClient(
        settings.jst_app_key,
        settings.jst_app_secret,
        settings.jst_token_file,
    )
    return feishu, jst


@app.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "log_dir": settings.log_dir}


@app.post("/api/v1/batch-push", response_model=BatchPushResponse)
def batch_push(
    request: Request,
    body: BatchPushRequest,
    _: Annotated[None, Depends(verify_submit_key)],
    settings: Settings = Depends(get_settings),
) -> BatchPushResponse:
    table_id = body.tableId.strip()
    if not table_id:
        raise HTTPException(status_code=400, detail="tableId 不能为空")

    feishu, jst = _clients(settings)
    request_id = str(uuid.uuid4())

    if _debouncer is None or not _debouncer.try_begin(table_id):
        logger.info("防抖拦截（处理中） table_id=%s request_id=%s", table_id, request_id)
        debounced = BatchPushResponse(
            ok=False,
            request_id=request_id,
            debounced=True,
            message="上一请求正在处理中，请等待完成后再试",
        )
        log_request_failure(request, debounced.model_dump(), trace_file="batch-push")
        return debounced

    try:
        try:
            result: BatchPushResult = run_batch_push(
                table_id=table_id,
                feishu=feishu,
                jst=jst,
                settings=settings,
                request_id=request_id,
            )
        except Exception as e:
            logger.exception("批量推送异常 table_id=%s request_id=%s", table_id, request_id)
            raise HTTPException(
                status_code=502,
                detail={"request_id": request_id, "error": str(e)},
            ) from e

        response = BatchPushResponse(
            ok=len(result.errors) == 0 and result.upload_failed == 0,
            request_id=result.request_id,
            message=result.message,
            detail={
                "build": "batch-push-v2",
                "table_id": result.table_id,
                "total_rows": result.total_rows,
                "dedup_rows": result.dedup_rows,
                "upload_attempted": result.upload_attempted,
                "upload_success": result.upload_success,
                "upload_failed": result.upload_failed,
                "logistic_attempted": result.logistic_attempted,
                "logistic_updated": result.logistic_updated,
                "errors": result.errors,
                "upload_results": result.upload_results,
                "steps": result.steps,
            },
        )
        log_request_failure(request, response.model_dump(), trace_file="batch-push")
        return response
    finally:
        if _debouncer is not None:
            _debouncer.end(table_id)


@app.post("/api/v1/push-jushuitan", response_model=SinglePushResponse)
def push_jushuitan(
    request: Request,
    recordId: Annotated[str | None, Query(description="飞书 record_id（query 传参）")] = None,
    record_id_query: Annotated[
        str | None, Query(alias="record_id", description="record_id 别名")
    ] = None,
    _: Annotated[None, Depends(verify_submit_key)] = None,
    settings: Settings = Depends(get_settings),
) -> SinglePushResponse:
    """
    单行推送聚水潭：飞书按钮/工作流触发。
    recordId 可通过：JSON body、query 参数、form 表单传递。
    """
    record_id = resolve_record_id(
        request,
        query_record_id=recordId,
        query_record_id_snake=record_id_query,
    )
    if not record_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "recordId 不能为空",
                "hint": '请传 JSON：{"recordId":"recXXX"}，或 URL：?recordId=recXXX',
            },
        )

    feishu, jst = _clients(settings)
    push_state = PushStateStore(settings.push_state_file)
    request_id = str(uuid.uuid4())
    debounce_key = f"single:{record_id}"

    if _debouncer is None or not _debouncer.try_begin(debounce_key):
        logger.info("防抖拦截（处理中） record_id=%s request_id=%s", record_id, request_id)
        debounced = SinglePushResponse(
            ok=False,
            request_id=request_id,
            debounced=True,
            message="该订单正在处理中，请稍后再试",
        )
        log_request_failure(request, debounced.model_dump(), trace_file="single-push")
        return debounced

    try:
        try:
            result: SinglePushResult = run_single_push(
                record_id=record_id,
                feishu=feishu,
                jst=jst,
                settings=settings,
                push_state=push_state,
                request_id=request_id,
            )
        except Exception as e:
            logger.exception("单行推送异常 record_id=%s request_id=%s", record_id, request_id)
            err = str(e)
            log_request_failure(
                request,
                {
                    "ok": False,
                    "request_id": request_id,
                    "message": err,
                    "detail": {
                        "build": "single-push-v1",
                        "record_id": record_id,
                        "error": err,
                        "upstream": "feishu/jushuitan",
                    },
                },
                trace_file="single-push",
            )
            raise HTTPException(
                status_code=502,
                detail={"request_id": request_id, "error": err},
            ) from e

        response = SinglePushResponse(
            ok=result.ok,
            request_id=result.request_id,
            message=result.message,
            detail={
                "build": "single-push-v1",
                "record_id": result.record_id,
                "so_id": result.so_id,
                "skipped_jst": result.skipped_jst,
                "jst_response": result.jst_response,
                "errors": result.errors,
                "steps": result.steps,
            },
        )
        log_request_failure(request, response.model_dump(), trace_file="single-push")
        return response
    finally:
        if _debouncer is not None:
            _debouncer.end(debounce_key)


@app.post("/api/v1/orders/sync", response_model=OrderSyncResponse)
def manual_order_sync(
    request: Request,
    body: OrderSyncRequest | None = None,
    _: Annotated[None, Depends(verify_submit_key)] = None,
    settings: Settings = Depends(get_settings),
) -> OrderSyncResponse:
    """
    手动同步聚水潭订单明细到飞书电商表（按 so_id + 69码 upsert）。
    - 传 year：同步该年全年
    - 传 begin + end：同步指定修改时间范围
    - 都不传：同步今年全年
    """
    feishu, jst = _clients(settings)
    index = OrderLineIndexStore(settings.order_line_index_file)
    payload = body if body is not None else OrderSyncRequest.model_validate({})

    if payload.begin and payload.end:
        modified_begin, modified_end = payload.begin.strip(), payload.end.strip()
    elif payload.year is not None:
        modified_begin, modified_end = year_range(payload.year)
    else:
        modified_begin, modified_end = year_range(datetime.now().year)

    table_id = (payload.tableId or settings.feishu_table_dianshang).strip()

    try:
        result: OrderSyncResult = run_order_sync(
            feishu=feishu,
            jst=jst,
            settings=settings,
            index_store=index,
            modified_begin=modified_begin,
            modified_end=modified_end,
            table_id=table_id,
        )
    except Exception as e:
        logger.exception("手动订单同步失败")
        raise HTTPException(status_code=502, detail=str(e)) from e

    response = OrderSyncResponse(
        ok=not result.errors,
        request_id=result.request_id,
        message=result.message,
        detail={
            "table_id": result.table_id,
            "modified_begin": result.modified_begin,
            "modified_end": result.modified_end,
            "orders_fetched": result.orders_fetched,
            "rows_built": result.rows_built,
            "rows_created": result.rows_created,
            "rows_updated": result.rows_updated,
            "errors": result.errors,
            "steps": result.steps,
        },
    )
    log_request_failure(request, response.model_dump(), trace_file="order-sync")
    return response
