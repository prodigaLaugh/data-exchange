from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.failure_log import log_batch_push_trace
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
    field_text,
    feishu_sync_time_value,
    field_int,
    field_money,
    format_order_date,
    parse_address,
)
from app.jushuitan.client import JushuitanClient
from app.jushuitan.shop_resolver import parse_shop_id
from app.jushuitan.sign import biz_json

logger = logging.getLogger(__name__)

UPLOAD_BATCH_SIZE = 20


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
    steps: list[dict[str, Any]] = field(default_factory=list)
    upload_results: list[dict[str, Any]] = field(default_factory=list)


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


def _build_jst_order(row: TableRow, *, shop_id: int) -> dict[str, Any]:
    f = row.fields
    address, name, phone = parse_address(f.get(COL_ADDRESS))
    channel = field_text(f.get(COL_CHANNEL))
    order_no = row.order_no
    item = {
        "sku_id": field_text(f.get(COL_BARCODE)),
        "shop_sku_id": field_text(f.get(COL_BARCODE)),
        "amount": field_money(f.get(COL_TOTAL_AMOUNT)),
        "base_price": field_money(f.get(COL_RETAIL_PRICE)),
        "price": field_money(f.get(COL_DISCOUNT_PRICE), decimals=4),
        "qty": field_int(f.get(COL_QTY)),
        "name": field_text(f.get(COL_PRODUCT_NAME)),
        "outer_oi_id": order_no,
    }
    order: dict[str, Any] = {
        "shop_id": shop_id,
        "so_id": order_no,
        "order_date": format_order_date(f.get(COL_ORDER_DATE)),
        "shop_status": "WAIT_SELLER_SEND_GOODS",
        "shop_buyer_id": str(shop_id),
        "receiver_address": address,
        "receiver_name": name,
        "receiver_phone": phone,
        "pay_amount": field_money(f.get(COL_TOTAL_AMOUNT)),
        "freight": field_money(f.get(COL_FREIGHT)),
        "items": [item],
    }
    return order


def _validate_receiver(address: str, name: str, phone: str) -> str | None:
    missing: list[str] = []
    if not address:
        missing.append("收货地址")
    if not name:
        missing.append("收货人")
    if not phone:
        missing.append("手机号")
    if missing:
        return f"收货信息不完整，缺少：{'、'.join(missing)}"
    return None


def _sanitize_jst_order(order: dict[str, Any]) -> dict[str, Any]:
    """日志/trace 用：脱敏地址电话，保留数值类型便于核对。"""
    import copy

    safe = copy.deepcopy(order)
    if safe.get("receiver_phone"):
        safe["receiver_phone"] = "***"
    addr = str(safe.get("receiver_address") or "")
    if len(addr) > 24:
        safe["receiver_address"] = addr[:24] + "..."
    name = str(safe.get("receiver_name") or "")
    if name:
        safe["receiver_name"] = name[0] + "**" if len(name) > 1 else "*"
    items = safe.get("items")
    if isinstance(items, list):
        safe["items"] = items
    return safe


def _truncate_fail_reason(msg: str, *, max_len: int = 500) -> str:
    if len(msg) <= max_len:
        return msg
    return msg[: max_len - 3] + "..."


def _summarize_jst_response(resp: dict[str, Any] | None) -> dict[str, Any]:
    if not resp:
        return {"so_id": "", "issuccess": False, "msg": "未返回该订单结果", "o_id": ""}
    return {
        "so_id": field_text(resp.get("so_id")),
        "issuccess": resp.get("issuccess"),
        "msg": field_text(resp.get("msg")),
        "o_id": field_text(resp.get("o_id")),
    }


def _summarize_feishu_updates(updates: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    sample: list[dict[str, Any]] = []
    for u in updates[:limit]:
        fields = u.get("fields") or {}
        sample.append(
            {
                "record_id": u.get("record_id"),
                "fields": dict(fields) if isinstance(fields, dict) else fields,
            }
        )
    return sample


class _PushTracer:
    def __init__(self, request_id: str, table_id: str) -> None:
        self.request_id = request_id
        self.table_id = table_id
        self.steps: list[dict[str, Any]] = []

    def record(self, step: str, ok: bool, **detail: Any) -> None:
        entry: dict[str, Any] = {"step": step, "ok": ok, **detail}
        self.steps.append(entry)
        logger.info(
            "batch_push step request_id=%s table_id=%s step=%s ok=%s detail=%s",
            self.request_id,
            self.table_id,
            step,
            ok,
            {k: v for k, v in detail.items() if k != "request_body"},
        )
        log_batch_push_trace(
            self.request_id,
            {"table_id": self.table_id, "trace_step": entry},
        )


def _make_status_update(
    index: dict[str, list[TableRow]],
    order_no: str,
    *,
    status: str,
    fail_reason: str = "",
    settings: Settings,
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for row in index.get(order_no, []):
        fields: dict[str, Any] = {
            COL_SYNC_STATUS: status,
            COL_SYNC_TIME: feishu_sync_time_value(
                row.fields.get(COL_SYNC_TIME),
                use_ms=settings.sync_time_use_ms,
            ),
        }
        if fail_reason:
            fields[COL_FAIL_REASON] = _truncate_fail_reason(fail_reason)
        updates.append({"record_id": row.record_id, "fields": fields})
    return updates


def run_batch_push(
    *,
    table_id: str,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
    request_id: str | None = None,
) -> BatchPushResult:
    request_id = request_id or str(uuid.uuid4())
    tracer = _PushTracer(request_id, table_id)
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
    tracer.record("start", True, table_id=table_id, build="batch-push-v2")

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

    tracer.record(
        "feishu_list",
        True,
        total_rows=result.total_rows,
        dedup_rows=result.dedup_rows,
        rows_with_order_no=sum(len(v) for v in index.values()),
        pending_upload=len(pending_upload),
        pending_upload_orders=[r.order_no for r in pending_upload],
    )

    feishu_updates: list[dict[str, Any]] = []

    # 1) 推送未成功的订单到聚水潭
    batch_no = 0
    for i in range(0, len(pending_upload), UPLOAD_BATCH_SIZE):
        batch = pending_upload[i : i + UPLOAD_BATCH_SIZE]
        batch_no += 1
        orders: list[dict[str, Any]] = []
        batch_skipped: list[dict[str, Any]] = []

        for row in batch:
            channel = field_text(row.fields.get(COL_CHANNEL))
            resolved_shop_id = parse_shop_id(channel)
            if resolved_shop_id is None:
                msg = f"渠道编码「{channel}」不是有效的聚水潭 shop_id（须为数字）"
                batch_skipped.append({"so_id": row.order_no, "channel": channel, "msg": msg})
                result.upload_attempted += 1
                result.upload_failed += 1
                result.upload_results.append(
                    {
                        "so_id": row.order_no,
                        "status": "failed",
                        "stage": "shop_id_resolve",
                        "msg": msg,
                    }
                )
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_failed,
                        fail_reason=msg,
                        settings=settings,
                    )
                )
                continue
            address, name, phone = parse_address(row.fields.get(COL_ADDRESS))
            recv_err = _validate_receiver(address, name, phone)
            if recv_err:
                batch_skipped.append({"so_id": row.order_no, "msg": recv_err})
                result.upload_attempted += 1
                result.upload_failed += 1
                result.upload_results.append(
                    {
                        "so_id": row.order_no,
                        "status": "failed",
                        "stage": "receiver_validate",
                        "msg": recv_err,
                    }
                )
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_failed,
                        fail_reason=recv_err,
                        settings=settings,
                    )
                )
                continue
            orders.append(_build_jst_order(row, shop_id=resolved_shop_id))

        if batch_skipped:
            tracer.record(
                "shop_id_resolve",
                False,
                batch=batch_no,
                skipped=batch_skipped,
            )

        if not orders:
            continue

        result.upload_attempted += len(orders)
        request_summary = {
            "batch": batch_no,
            "order_count": len(orders),
            "so_ids": [o["so_id"] for o in orders],
            "orders": [_sanitize_jst_order(o) for o in orders],
            "biz_json_sample": biz_json(orders[:1])[:800],
        }
        try:
            responses = jst.upload_orders(orders)
        except Exception as e:
            logger.exception("订单上传批次失败 request_id=%s batch=%s", request_id, batch_no)
            err = str(e)
            result.errors.append(err)
            tracer.record(
                "jst_upload",
                False,
                batch=batch_no,
                request=request_summary,
                error=err,
            )
            for row in batch:
                result.upload_results.append(
                    {
                        "so_id": row.order_no,
                        "status": "failed",
                        "stage": "jst_upload_http",
                        "msg": err,
                    }
                )
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_failed,
                        fail_reason=err,
                        settings=settings,
                    )
                )
                result.upload_failed += 1
            continue

        response_summary = [_summarize_jst_response(r) for r in responses]
        batch_ok = True
        resp_map = {field_text(r.get("so_id")): r for r in responses}
        batch_order_results: list[dict[str, Any]] = []

        for row in batch:
            resp = resp_map.get(row.order_no)
            summary = _summarize_jst_response(resp)
            if resp and resp.get("issuccess") is True:
                batch_order_results.append(
                    {**summary, "status": "success", "stage": "jst_upload"}
                )
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_success,
                        settings=settings,
                    )
                )
                result.upload_success += 1
            else:
                batch_ok = False
                msg = summary["msg"] or "同步失败"
                batch_order_results.append(
                    {**summary, "status": "failed", "stage": "jst_upload", "msg": msg}
                )
                feishu_updates.extend(
                    _make_status_update(
                        index,
                        row.order_no,
                        status=settings.sync_status_failed,
                        fail_reason=msg,
                        settings=settings,
                    )
                )
                result.upload_failed += 1

        result.upload_results.extend(batch_order_results)
        tracer.record(
            "jst_upload",
            batch_ok,
            batch=batch_no,
            request=request_summary,
            response=response_summary,
            order_results=batch_order_results,
        )

    if feishu_updates:
        update_summary = {
            "update_count": len(feishu_updates),
            "sample": _summarize_feishu_updates(feishu_updates),
        }
        try:
            updated = feishu.batch_update_records(table_id, feishu_updates)
            tracer.record("feishu_update", True, **update_summary, rows_updated=updated)
        except Exception as e:
            err = str(e)
            tracer.record("feishu_update", False, **update_summary, error=err)
            log_batch_push_trace(
                request_id,
                {
                    "table_id": table_id,
                    "event": "failed",
                    "error": err,
                    "steps": tracer.steps,
                },
            )
            raise
    else:
        tracer.record("feishu_update", True, skipped=True, reason="无待回写记录")

    result.steps = tracer.steps
    result.message = (
        f"推送完成：上传 {result.upload_attempted} 条（成功 {result.upload_success}，"
        f"失败 {result.upload_failed}）"
    )
    log_batch_push_trace(
        request_id,
        {
            "table_id": table_id,
            "event": "completed",
            "message": result.message,
            "upload_attempted": result.upload_attempted,
            "upload_success": result.upload_success,
            "upload_failed": result.upload_failed,
            "errors": result.errors,
            "steps": tracer.steps,
        },
    )
    logger.info("批量推送结束 request_id=%s %s", request_id, result.message)
    return result
