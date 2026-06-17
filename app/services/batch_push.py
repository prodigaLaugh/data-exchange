from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.feishu.client import FeishuClient
from app.fields import (
    COL_ADDRESS,
    COL_BARCODE,
    COL_CHANNEL,
    COL_DISCOUNT_PRICE,
    COL_FAIL_REASON,
    COL_FREIGHT,
    COL_ORDER_DATE,
    COL_ORDER_NO,
    COL_PRODUCT_NAME,
    COL_QTY,
    COL_RETAIL_PRICE,
    COL_SYNC_STATUS,
    COL_SYNC_TIME,
    COL_TOTAL_AMOUNT,
    COL_TRACKING_NO,
    field_text,
    format_order_date,
    now_sync_time_ms,
    parse_address,
)
from app.jushuitan.client import JushuitanClient

logger = logging.getLogger(__name__)

UPLOAD_BATCH_SIZE = 20
LOGISTIC_BATCH_SIZE = 50


@dataclass
class TableRow:
    record_id: str
    order_no: str
    fields: dict[str, Any]


@dataclass
class BatchPushResult:
    request_id: str
    table_id: str
    total_rows: int
    dedup_rows: int
    upload_attempted: int
    upload_success: int
    upload_failed: int
    logistic_attempted: int
    logistic_updated: int
    debounced: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


def _index_by_order_no(records: list[dict[str, Any]]) -> dict[str, list[TableRow]]:
    """订单编号 -> 行列表（更新时按订单号定位所有行）。"""
    index: dict[str, list[TableRow]] = {}
    for rec in records:
        record_id = str(rec.get("record_id") or "")
        fields = rec.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            continue
        order_no = field_text(fields.get(COL_ORDER_NO))
        if not order_no:
            continue
        row = TableRow(record_id=record_id, order_no=order_no, fields=fields)
        index.setdefault(order_no, []).append(row)
    return index


def _dedup_rows(index: dict[str, list[TableRow]]) -> dict[str, TableRow]:
    """按订单编号去重，保留第一条。"""
    deduped: dict[str, TableRow] = {}
    for order_no, rows in index.items():
        deduped[order_no] = rows[0]
    return deduped


def _build_jst_order(row: TableRow) -> dict[str, Any]:
    f = row.fields
    address, name, phone = parse_address(f.get(COL_ADDRESS))
    channel = field_text(f.get(COL_CHANNEL))
    order_no = row.order_no
    item = {
        "sku_id": field_text(f.get(COL_BARCODE)),
        "shop_sku_id": field_text(f.get(COL_BARCODE)),
        "amount": field_text(f.get(COL_TOTAL_AMOUNT)),
        "base_price": field_text(f.get(COL_RETAIL_PRICE)),
        "price": field_text(f.get(COL_DISCOUNT_PRICE)),
        "qty": field_text(f.get(COL_QTY)),
        "name": field_text(f.get(COL_PRODUCT_NAME)),
        "outer_oi_id": order_no,
    }
    return {
        "shop_id": channel,
        "so_id": order_no,
        "order_date": format_order_date(f.get(COL_ORDER_DATE)),
        "shop_status": "WAIT_SELLER_SEND_GOODS",
        "shop_buyer_id": channel,
        "receiver_address": address,
        "receiver_name": name,
        "receiver_phone": phone,
        "pay_amount": field_text(f.get(COL_TOTAL_AMOUNT)),
        "freight": field_text(f.get(COL_FREIGHT)),
        "items": [item],
    }


def _sync_time_field() -> int:
    return now_sync_time_ms()


def _make_status_update(
    index: dict[str, list[TableRow]],
    order_no: str,
    *,
    status: str,
    fail_reason: str = "",
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    sync_time = _sync_time_field()
    for row in index.get(order_no, []):
        fields: dict[str, Any] = {
            COL_SYNC_STATUS: status,
            COL_SYNC_TIME: sync_time,
        }
        if fail_reason:
            fields[COL_FAIL_REASON] = fail_reason
        else:
            fields[COL_FAIL_REASON] = ""
        updates.append({"record_id": row.record_id, "fields": fields})
    return updates


def run_batch_push(
    *,
    table_id: str,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
) -> BatchPushResult:
    request_id = str(uuid.uuid4())
    result = BatchPushResult(
        request_id=request_id,
        table_id=table_id,
        total_rows=0,
        dedup_rows=0,
        upload_attempted=0,
        upload_success=0,
        upload_failed=0,
        logistic_attempted=0,
        logistic_updated=0,
    )

    logger.info("开始批量推送 request_id=%s table_id=%s", request_id, table_id)
    records = feishu.list_all_records(table_id)
    result.total_rows = len(records)
    index = _index_by_order_no(records)
    deduped = _dedup_rows(index)
    result.dedup_rows = len(deduped)

    pending_upload = [
        row
        for row in deduped.values()
        if field_text(row.fields.get(COL_SYNC_STATUS)) != settings.sync_status_success
    ]
    pending_logistic = [
        row
        for row in deduped.values()
        if field_text(row.fields.get(COL_SYNC_STATUS)) == settings.sync_status_success
        and not field_text(row.fields.get(COL_TRACKING_NO))
    ]

    feishu_updates: list[dict[str, Any]] = []

    # 1) 推送未成功的订单到聚水潭
    for i in range(0, len(pending_upload), UPLOAD_BATCH_SIZE):
        batch = pending_upload[i : i + UPLOAD_BATCH_SIZE]
        orders = [_build_jst_order(r) for r in batch]
        result.upload_attempted += len(batch)
        try:
            responses = jst.upload_orders(orders)
        except Exception as e:
            logger.exception("订单上传批次失败 request_id=%s", request_id)
            result.errors.append(str(e))
            for row in batch:
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_failed,
                        fail_reason=str(e),
                    )
                )
                result.upload_failed += 1
            continue

        resp_map = {field_text(r.get("so_id")): r for r in responses}
        for row in batch:
            resp = resp_map.get(row.order_no)
            if resp and resp.get("issuccess") is True:
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_success,
                    )
                )
                result.upload_success += 1
            else:
                msg = field_text(resp.get("msg") if resp else "未返回该订单结果")
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_failed,
                        fail_reason=msg or "推送失败",
                    )
                )
                result.upload_failed += 1

    # 2) 查询已推送成功但无快递单号的订单物流
    so_ids = [r.order_no for r in pending_logistic]
    result.logistic_attempted = len(so_ids)
    tracking_map: dict[str, str] = {}

    for i in range(0, len(so_ids), LOGISTIC_BATCH_SIZE):
        chunk = so_ids[i : i + LOGISTIC_BATCH_SIZE]
        try:
            logistics = jst.query_logistics(chunk)
        except Exception as e:
            logger.exception("物流查询失败 request_id=%s chunk=%s", request_id, chunk[:3])
            result.errors.append(str(e))
            continue
        for item in logistics:
            so_id = field_text(item.get("so_id"))
            l_id = field_text(item.get("l_id") or item.get("logistics_no") or item.get("快递单号"))
            if so_id and l_id:
                tracking_map[so_id] = l_id

    for order_no, tracking_no in tracking_map.items():
        for row in index.get(order_no, []):
            feishu_updates.append(
                {
                    "record_id": row.record_id,
                    "fields": {COL_TRACKING_NO: tracking_no},
                }
            )
            result.logistic_updated += 1

    if feishu_updates:
        feishu.batch_update_records(table_id, feishu_updates)

    result.message = (
        f"推送完成：上传 {result.upload_attempted} 条（成功 {result.upload_success}，"
        f"失败 {result.upload_failed}）；物流查询 {result.logistic_attempted} 条，"
        f"回写快递单号 {result.logistic_updated} 行"
    )
    logger.info("批量推送结束 request_id=%s %s", request_id, result.message)
    return result
