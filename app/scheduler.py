from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings, get_settings
from app.failure_log import log_failure
from app.feishu.client import FeishuClient
from app.jushuitan.client import JushuitanClient
from app.order_line_index import OrderLineIndexStore
from app.services.logistics_sync import run_logistics_sync
from app.services.order_sync import run_weekly_order_sync

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _build_clients(settings: Settings) -> tuple[FeishuClient, JushuitanClient]:
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


def _weekly_order_sync_job() -> None:
    settings = get_settings()
    feishu, jst = _build_clients(settings)
    index = OrderLineIndexStore(settings.order_line_index_file)
    try:
        result = run_weekly_order_sync(
            feishu=feishu,
            jst=jst,
            settings=settings,
            index_store=index,
        )
        if result.errors:
            log_failure(
                request_url="scheduler://weekly_order_sync",
                request_method="JOB",
                response={
                    "message": result.message,
                    "request_id": result.request_id,
                    "table_id": result.table_id,
                    "modified_begin": result.modified_begin,
                    "modified_end": result.modified_end,
                    "errors": result.errors,
                },
            )
    except Exception as e:
        logger.exception("每周订单同步任务执行失败")
        log_failure(
            request_url="scheduler://weekly_order_sync",
            request_method="JOB",
            response={"error": str(e)},
        )


def _logistics_job() -> None:
    settings = get_settings()
    feishu, jst = _build_clients(settings)
    try:
        result = run_logistics_sync(feishu=feishu, jst=jst, settings=settings)
        if result.errors:
            log_failure(
                request_url="scheduler://logistics_sync",
                request_method="JOB",
                response={
                    "message": result.message,
                    "request_id": result.request_id,
                    "table_id": result.table_id,
                    "errors": result.errors,
                },
            )
    except Exception as e:
        logger.exception("定时物流同步任务执行失败")
        log_failure(
            request_url="scheduler://logistics_sync",
            request_method="JOB",
            response={"error": str(e)},
        )


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        _weekly_order_sync_job,
        CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="weekly_order_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        _logistics_job,
        CronTrigger(hour=0, minute=0),
        id="logistics_sync",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("已启动定时任务：每周一 03:00 电商订单明细同步；每日 00:00 物流回写")
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
