from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.feishu.client import FeishuClient
from app.fields import COL_ORDER_NO, COL_SYNC_STATUS, COL_TRACKING_NO, field_text
from app.jushuitan.client import JushuitanClient
from app.services.batch_push import _index_by_order_no

logger = logging.getLogger(__name__)

LOGISTIC_BATCH_SIZE = 50


@dataclass
class LogisticsSyncResult:
    request_id: str
    table_id: str
    total_rows: int
    eligible_orders: int
    logistic_attempted: int
    logistic_updated: int
    message: str = ""
    errors: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    tracking_results: list[dict[str, Any]] = field(default_factory=list)


class _LogisticsTracer:
    def __init__(self, request_id: str, table_id: str) -> None:
        self.request_id = request_id
        self.table_id = table_id
        self.steps: list[dict[str, Any]] = []

    def record(self, step: str, ok: bool, **detail: Any) -> None:
        entry: dict[str, Any] = {"step": step, "ok": ok, **detail}
        self.steps.append(entry)
        logger.info(
            "logistics_sync step request_id=%s table_id=%s step=%s ok=%s detail=%s",
            self.request_id,
            self.table_id,
            step,
            ok,
            detail,
        )


def run_logistics_sync(
    *,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
    table_id: str | None = None,
    request_id: str | None = None,
) -> LogisticsSyncResult:
    """
    读取文创表「同步成功」且快递单号为空的订单，查聚水潭物流并回写飞书。
    https://openweb.jushuitan.com/dev-doc?docType=5&docId=25
    """
    request_id = request_id or str(uuid.uuid4())
    table_id = table_id or settings.feishu_table_wenchuang
    tracer = _LogisticsTracer(request_id, table_id)
    result = LogisticsSyncResult(
        request_id=request_id,
        table_id=table_id,
        total_rows=0,
        eligible_orders=0,
        logistic_attempted=0,
        logistic_updated=0,
    )

    logger.info("开始物流同步 request_id=%s table_id=%s", request_id, table_id)
    tracer.record("start", True, table_id=table_id)

    records = feishu.list_all_records(table_id)
    result.total_rows = len(records)
    index = _index_by_order_no(records)

    eligible_so_ids: list[str] = []
    seen: set[str] = set()
    for rows in index.values():
        for row in rows:
            status = field_text(row.fields.get(COL_SYNC_STATUS))
            tracking = field_text(row.fields.get(COL_TRACKING_NO))
            if status != settings.sync_status_success or tracking:
                continue
            if row.order_no in seen:
                continue
            seen.add(row.order_no)
            eligible_so_ids.append(row.order_no)

    result.eligible_orders = len(eligible_so_ids)
    tracer.record(
        "feishu_list",
        True,
        total_rows=result.total_rows,
        eligible_orders=result.eligible_orders,
        order_nos=eligible_so_ids,
    )

    if not eligible_so_ids:
        result.message = "无待查物流订单（同步成功且快递单号为空）"
        result.steps = tracer.steps
        logger.info("物流同步结束 request_id=%s %s", request_id, result.message)
        return result

    tracking_map: dict[str, str] = {}
    feishu_updates: list[dict[str, Any]] = []
    batch_no = 0

    for i in range(0, len(eligible_so_ids), LOGISTIC_BATCH_SIZE):
        chunk = eligible_so_ids[i : i + LOGISTIC_BATCH_SIZE]
        batch_no += 1
        result.logistic_attempted += len(chunk)
        try:
            logistics = jst.query_logistics(chunk)
        except Exception as e:
            err = str(e)
            logger.exception("物流查询失败 request_id=%s batch=%s", request_id, batch_no)
            result.errors.append(err)
            tracer.record(
                "jst_logistics",
                False,
                batch=batch_no,
                request={"so_ids": chunk},
                error=err,
            )
            continue

        chunk_results: list[dict[str, Any]] = []
        for item in logistics:
            so_id = field_text(item.get("so_id"))
            l_id = field_text(
                item.get("l_id") or item.get("logistics_no") or item.get("快递单号")
            )
            chunk_results.append(
                {
                    "so_id": so_id,
                    "l_id": l_id or None,
                    "has_tracking": bool(so_id and l_id),
                }
            )
            if so_id and l_id:
                tracking_map[so_id] = l_id

        tracer.record(
            "jst_logistics",
            True,
            batch=batch_no,
            request={"so_ids": chunk},
            response=chunk_results,
            tracking_found=sum(1 for r in chunk_results if r.get("has_tracking")),
        )

    for order_no, tracking_no in tracking_map.items():
        updated_rows = 0
        for row in index.get(order_no, []):
            feishu_updates.append(
                {
                    "record_id": row.record_id,
                    "fields": {COL_TRACKING_NO: tracking_no},
                }
            )
            updated_rows += 1
            result.logistic_updated += 1
        result.tracking_results.append(
            {
                "so_id": order_no,
                "tracking_no": tracking_no,
                "rows_updated": updated_rows,
                "status": "updated",
            }
        )

    for order_no in eligible_so_ids:
        if order_no not in tracking_map:
            result.tracking_results.append(
                {"so_id": order_no, "tracking_no": None, "status": "no_tracking_yet"}
            )

    if feishu_updates:
        try:
            updated = feishu.batch_update_records(table_id, feishu_updates)
            tracer.record(
                "feishu_update",
                True,
                update_count=len(feishu_updates),
                rows_updated=updated,
                sample=feishu_updates[:5],
            )
        except Exception as e:
            err = str(e)
            tracer.record("feishu_update", False, update_count=len(feishu_updates), error=err)
            result.errors.append(err)
            result.steps = tracer.steps
            raise
    else:
        tracer.record("feishu_update", True, skipped=True, reason="聚水潭未返回可回写的快递单号")

    result.steps = tracer.steps
    result.message = (
        f"物流同步完成：待查 {result.eligible_orders} 单，"
        f"回写快递单号 {result.logistic_updated} 行"
    )
    logger.info("物流同步结束 request_id=%s %s", request_id, result.message)
    return result
