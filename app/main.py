from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.debounce import Debouncer
from app.failure_log import log_failure
from app.feishu.client import FeishuClient
from app.jushuitan.client import JushuitanClient
from app.scheduler import shutdown_scheduler, start_scheduler
from app.services.batch_push import BatchPushResult, run_batch_push
from app.services.monthly_revenue import MonthlyRevenueResult, run_monthly_revenue_sync

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
    setup_logging(settings.log_level)
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    global _debouncer
    _debouncer = Debouncer(settings.debounce_seconds)
    start_scheduler()
    logger.info("服务已启动")
    yield
    shutdown_scheduler()
    logger.info("服务已停止")


app = FastAPI(
    title="飞书多维表 ↔ 聚水潭对接",
    version="1.0.0",
    lifespan=lifespan,
)


class BatchPushRequest(BaseModel):
    tableId: str = Field(..., description="飞书多维表格 table_id")


class BatchPushResponse(BaseModel):
    ok: bool
    request_id: str
    debounced: bool = False
    message: str
    detail: dict[str, Any] | None = None


class MonthlySyncResponse(BaseModel):
    ok: bool
    request_id: str
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
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/batch-push", response_model=BatchPushResponse)
def batch_push(
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
        return BatchPushResponse(
            ok=False,
            request_id=request_id,
            debounced=True,
            message="上一请求正在处理中，请等待完成后再试",
        )

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
            log_failure(
                "api",
                str(e),
                path="/api/v1/batch-push",
                context={"request_id": request_id, "table_id": table_id},
                exc=e,
            )
            raise HTTPException(
                status_code=502,
                detail={"request_id": request_id, "error": str(e)},
            ) from e

        if result.errors:
            log_failure(
                "batch_push",
                "批量推送部分步骤失败",
                path="/api/v1/batch-push",
                context={
                    "request_id": result.request_id,
                    "table_id": table_id,
                    "errors": result.errors,
                    "steps": result.steps,
                },
            )

        return BatchPushResponse(
            ok=len(result.errors) == 0 and result.upload_failed == 0,
            request_id=result.request_id,
            message=result.message,
            detail={
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
    finally:
        if _debouncer is not None:
            _debouncer.end(table_id)


@app.post("/api/v1/monthly-revenue/sync", response_model=MonthlySyncResponse)
def manual_monthly_sync(
    _: Annotated[None, Depends(verify_submit_key)],
    settings: Settings = Depends(get_settings),
) -> MonthlySyncResponse:
    """手动触发月度营收汇总（定时任务为每月 6 日自动执行）。"""
    feishu, jst = _clients(settings)
    try:
        result: MonthlyRevenueResult = run_monthly_revenue_sync(
            feishu=feishu,
            jst=jst,
            settings=settings,
        )
    except Exception as e:
        logger.exception("手动月度汇总失败")
        log_failure(
            "api",
            str(e),
            path="/api/v1/monthly-revenue/sync",
            exc=e,
        )
        raise HTTPException(status_code=502, detail=str(e)) from e

    if result.errors:
        log_failure(
            "monthly_revenue",
            result.message,
            path="/api/v1/monthly-revenue/sync",
            context={
                "request_id": result.request_id,
                "target_month": result.target_month,
                "errors": result.errors,
            },
        )

    return MonthlySyncResponse(
        ok=not result.errors,
        request_id=result.request_id,
        message=result.message,
        detail={
            "target_month": result.target_month,
            "orders_fetched": result.orders_fetched,
            "product_groups": result.product_groups,
            "rows_appended": result.rows_appended,
            "errors": result.errors,
        },
    )
