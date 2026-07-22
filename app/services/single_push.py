from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.feishu.client import FeishuClient
from app.fields import (
    COL_APPLY_DATE,
    COL_CHANNEL,
    COL_BARCODE,
    COL_DISCOUNT_PRICE,
    COL_EXPRESS_ADDRESS,
    COL_INVOICE_AMOUNT,
    COL_LINKED_PRODUCTS,
    COL_PRODUCT_NAME,
    COL_QTY,
    COL_REMARK,
    COL_RETAIL_PRICE,
    COL_SUB_ORDER_NO,
    COL_SYNC_STATUS,
    COL_SYNC_TIME,
    extract_link_record_ids,
    feishu_sync_time_value,
    resolve_sync_time_use_ms,
    field_int,
    field_money,
    field_text,
    format_order_date,
    parse_express_address,
    row_apply_date,
    row_so_id,
)
from app.jushuitan.client import JushuitanClient
from app.jushuitan.shop_resolver import parse_shop_id
from app.jushuitan.sign import biz_json
from app.push_state import PushStateStore
from app.services.batch_push import (
    _sanitize_jst_order,
    _summarize_jst_response,
    _truncate_fail_reason,
    _validate_receiver,
)

logger = logging.getLogger(__name__)


@dataclass
class SinglePushResult:
    request_id: str
    record_id: str
    so_id: str
    ok: bool
    skipped_jst: bool = False
    message: str = ""
    jst_response: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)


class _SinglePushTracer:
    def __init__(self, request_id: str, record_id: str) -> None:
        self.request_id = request_id
        self.record_id = record_id
        self.steps: list[dict[str, Any]] = []

    def record(self, step: str, ok: bool, **detail: Any) -> None:
        entry: dict[str, Any] = {"step": step, "ok": ok, **detail}
        self.steps.append(entry)
        logger.info(
            "single_push step request_id=%s record_id=%s step=%s ok=%s detail=%s",
            self.request_id,
            self.record_id,
            step,
            ok,
            {k: v for k, v in detail.items() if k != "request_body"},
        )


def _shop_sku_id(*, apply_no: str, item_fields: dict[str, Any]) -> str:
    sub_order_no = field_text(item_fields.get(COL_SUB_ORDER_NO))
    if sub_order_no:
        return sub_order_no
    barcode = field_text(item_fields.get(COL_BARCODE))
    if apply_no and barcode:
        return f"{apply_no}_{barcode}"
    return barcode or apply_no


def _build_jst_items(apply_no: str, item_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for rec in item_records:
        f = rec.get("fields") or {}
        if not isinstance(f, dict):
            continue
        barcode = field_text(f.get(COL_BARCODE))
        if not barcode:
            continue
        qty = field_int(f.get(COL_QTY))
        price = field_money(f.get(COL_DISCOUNT_PRICE), decimals=4)
        base_price = field_money(f.get(COL_RETAIL_PRICE))
        shop_sku = _shop_sku_id(apply_no=apply_no, item_fields=f)
        amount = field_money(price * qty if qty else price)
        item: dict[str, Any] = {
            "sku_id": barcode,
            "shop_sku_id": shop_sku,
            "amount": amount,
            "base_price": base_price,
            "price": price,
            "qty": qty,
            "name": field_text(f.get(COL_PRODUCT_NAME)),
            "outer_oi_id": shop_sku,
        }
        remark = field_text(f.get(COL_REMARK))
        if remark:
            item["remark"] = remark
        items.append(item)
    return items


def _build_jst_pay(*, apply_no: str, order_date: str, pay_amount: float) -> dict[str, Any]:
    """聚水潭 WAIT_SELLER_SEND_GOODS 需带 pay 节点，且 pay.amount 须与 pay_amount 一致。"""
    return {
        "outer_pay_id": f"{apply_no}pay",
        "pay_date": order_date,
        "payment": "微信",
        "seller_account": "seller_account",
        "buyer_account": "buyer_account",
        "amount": pay_amount,
    }


def _build_jst_order_from_parent(
    *,
    apply_no: str,
    parent_fields: dict[str, Any],
    item_records: list[dict[str, Any]],
    shop_id: int,
) -> dict[str, Any]:
    address, name, phone = parse_express_address(parent_fields.get(COL_EXPRESS_ADDRESS))
    items = _build_jst_items(apply_no, item_records)
    order_date = format_order_date(row_apply_date(parent_fields))
    pay_amount = field_money(parent_fields.get(COL_INVOICE_AMOUNT))
    order: dict[str, Any] = {
        "shop_id": shop_id,
        "so_id": apply_no,
        "order_date": order_date,
        "shop_status": "WAIT_SELLER_SEND_GOODS",
        "shop_buyer_id": str(shop_id),
        "receiver_address": address,
        "receiver_name": name,
        "receiver_phone": phone,
        "pay_amount": pay_amount,
        "pay": _build_jst_pay(apply_no=apply_no, order_date=order_date, pay_amount=pay_amount),
        "freight": 0.0,
        "items": items,
    }
    remark = field_text(parent_fields.get(COL_REMARK))
    if remark:
        order["remark"] = remark
    return order


def _make_feishu_status_update(
    record_id: str,
    existing_fields: dict[str, Any],
    *,
    status: str,
    sync_reason: str = "",
    settings: Settings,
    feishu: FeishuClient,
    table_id: str,
) -> dict[str, Any]:
    use_ms = resolve_sync_time_use_ms(
        existing_fields.get(COL_SYNC_TIME),
        settings_use_ms=settings.sync_time_use_ms,
        field_is_datetime=feishu.is_datetime_field(table_id, COL_SYNC_TIME),
    )
    fields: dict[str, Any] = {
        COL_SYNC_STATUS: status,
        COL_SYNC_TIME: feishu_sync_time_value(
            existing_fields.get(COL_SYNC_TIME),
            use_ms=use_ms,
        ),
    }
    if sync_reason:
        fields[settings.feishu_col_sync_reason] = _truncate_fail_reason(sync_reason)
    return {"record_id": record_id, "fields": fields}


def _fetch_linked_items(
    feishu: FeishuClient,
    *,
    parent_table_id: str,
    parent_fields: dict[str, Any],
    items_table_id: str,
) -> list[dict[str, Any]]:
    link_ids = extract_link_record_ids(parent_fields.get(COL_LINKED_PRODUCTS))
    if not link_ids:
        return []
    if not items_table_id:
        raise ValueError("未配置关联子表 table_id，且无法从字段元数据解析")
    return feishu.batch_get_records(items_table_id, link_ids)


def run_single_push(
    *,
    record_id: str,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
    push_state: PushStateStore,
    request_id: str | None = None,
) -> SinglePushResult:
    request_id = request_id or str(uuid.uuid4())
    table_id = settings.feishu_table_wenchuang
    tracer = _SinglePushTracer(request_id, record_id)
    result = SinglePushResult(
        request_id=request_id,
        record_id=record_id,
        so_id="",
        ok=False,
    )

    logger.info("开始单行推送 request_id=%s record_id=%s", request_id, record_id)
    tracer.record("start", True, table_id=table_id, build="single-push-v1")

    try:
        record = feishu.get_record(table_id, record_id)
    except Exception as e:
        err = str(e)
        tracer.record(
            "feishu_get",
            False,
            error=err,
            table_id=table_id,
            record_id=record_id,
        )
        raise

    parent_fields = record.get("fields") or {}
    if not isinstance(parent_fields, dict):
        msg = "父表记录缺少 fields"
        result.message = msg
        result.errors.append(msg)
        tracer.record("feishu_get", False, error=msg)
        result.steps = tracer.steps
        return result

    apply_no = row_so_id(parent_fields)
    result.so_id = apply_no
    if not apply_no:
        msg = "申请编号为空，无法推送"
        result.message = msg
        result.errors.append(msg)
        tracer.record("validate", False, error=msg)
        result.steps = tracer.steps
        return result

    current_status = field_text(parent_fields.get(COL_SYNC_STATUS))
    if current_status == settings.sync_status_success:
        result.ok = True
        result.message = "该订单已同步成功，无需重复推送"
        tracer.record("skip", True, reason="already_synced", so_id=apply_no)
        result.steps = tracer.steps
        return result

    items_table_id = feishu.resolve_linked_table_id(
        table_id,
        COL_LINKED_PRODUCTS,
        fallback_table_id=settings.feishu_table_wenchuang_items.strip(),
    )
    link_ids = extract_link_record_ids(parent_fields.get(COL_LINKED_PRODUCTS))
    try:
        item_records = _fetch_linked_items(
            feishu,
            parent_table_id=table_id,
            parent_fields=parent_fields,
            items_table_id=items_table_id,
        )
    except Exception as e:
        msg = str(e)
        result.message = msg
        result.errors.append(msg)
        tracer.record("feishu_items", False, error=msg, link_record_ids=link_ids)
        result.steps = tracer.steps
        return result

    tracer.record(
        "feishu_get",
        True,
        so_id=apply_no,
        items_table_id=items_table_id,
        link_record_ids=link_ids,
        item_count=len(item_records),
        sync_status=current_status,
        **(
            {"raw_linked_field": parent_fields.get(COL_LINKED_PRODUCTS)}
            if not link_ids
            else {}
        ),
    )

    if not item_records:
        msg = (
            "关联文创营收无有效商品行，无法推送"
            f"（关联记录 {len(link_ids)} 条，子表 {items_table_id}）"
        )
        update = _make_feishu_status_update(
            record_id,
            parent_fields,
            status=settings.sync_status_failed,
            sync_reason=msg,
            settings=settings,
            feishu=feishu,
            table_id=table_id,
        )
        try:
            feishu.batch_update_records(table_id, [update])
        except Exception as e:
            result.errors.append(str(e))
        result.message = msg
        tracer.record("validate", False, error=msg)
        result.steps = tracer.steps
        return result

    channel = field_text(parent_fields.get(COL_CHANNEL))
    resolved_shop_id = parse_shop_id(channel)
    if resolved_shop_id is None:
        msg = f"渠道编码「{channel}」不是有效的聚水潭 shop_id（须为数字）"
        update = _make_feishu_status_update(
            record_id,
            parent_fields,
            status=settings.sync_status_failed,
            sync_reason=msg,
            settings=settings,
            feishu=feishu,
            table_id=table_id,
        )
        feishu.batch_update_records(table_id, [update])
        result.message = msg
        result.errors.append(msg)
        tracer.record("shop_id_resolve", False, channel=channel, error=msg)
        result.steps = tracer.steps
        return result

    recv_err = _validate_receiver(parent_fields.get(COL_EXPRESS_ADDRESS))
    if recv_err:
        update = _make_feishu_status_update(
            record_id,
            parent_fields,
            status=settings.sync_status_failed,
            sync_reason=recv_err,
            settings=settings,
            feishu=feishu,
            table_id=table_id,
        )
        feishu.batch_update_records(table_id, [update])
        result.message = recv_err
        result.errors.append(recv_err)
        tracer.record("receiver_validate", False, error=recv_err)
        result.steps = tracer.steps
        return result

    order = _build_jst_order_from_parent(
        apply_no=apply_no,
        parent_fields=parent_fields,
        item_records=item_records,
        shop_id=resolved_shop_id,
    )
    if not order.get("items"):
        msg = "子表商品行缺少有效 69码，无法推送"
        update = _make_feishu_status_update(
            record_id,
            parent_fields,
            status=settings.sync_status_failed,
            sync_reason=msg,
            settings=settings,
            feishu=feishu,
            table_id=table_id,
        )
        feishu.batch_update_records(table_id, [update])
        result.message = msg
        tracer.record("validate", False, error=msg)
        result.steps = tracer.steps
        return result

    jst_summary: dict[str, Any] | None = None
    skip_jst = push_state.is_jst_success(apply_no)

    if skip_jst:
        result.skipped_jst = True
        stored = push_state.get(apply_no) or {}
        jst_summary = {
            "so_id": apply_no,
            "issuccess": True,
            "msg": field_text(stored.get("msg")) or "聚水潭已推送成功（本地记录），仅重试飞书回写",
            "o_id": field_text(stored.get("o_id")),
        }
        tracer.record(
            "jst_upload",
            True,
            skipped=True,
            reason="push_state_jst_success",
            stored=stored,
        )
    else:
        request_summary = {
            "so_id": apply_no,
            "order": _sanitize_jst_order(order),
            "biz_json_sample": biz_json([order])[:800],
        }
        try:
            responses = jst.upload_orders([order])
        except Exception as e:
            err = str(e)
            logger.exception("单行订单上传失败 request_id=%s so_id=%s", request_id, apply_no)
            update = _make_feishu_status_update(
                record_id,
                parent_fields,
                status=settings.sync_status_failed,
                sync_reason=err,
                settings=settings,
                feishu=feishu,
                table_id=table_id,
            )
            try:
                feishu.batch_update_records(table_id, [update])
            except Exception as fe:
                err = f"{err}；飞书回写也失败: {fe}"
            result.message = err
            result.errors.append(err)
            tracer.record("jst_upload", False, request=request_summary, error=err)
            result.steps = tracer.steps
            return result

        resp = responses[0] if responses else None
        jst_summary = _summarize_jst_response(resp)
        if not resp or resp.get("issuccess") is not True:
            msg = jst_summary.get("msg") or "同步失败"
            update = _make_feishu_status_update(
                record_id,
                parent_fields,
                status=settings.sync_status_failed,
                sync_reason=msg,
                settings=settings,
                feishu=feishu,
                table_id=table_id,
            )
            feishu.batch_update_records(table_id, [update])
            result.message = msg
            result.errors.append(msg)
            result.jst_response = jst_summary
            tracer.record(
                "jst_upload",
                False,
                request=request_summary,
                response=jst_summary,
            )
            result.steps = tracer.steps
            return result

        push_state.mark_jst_success(
            apply_no,
            record_id=record_id,
            o_id=field_text(jst_summary.get("o_id")),
            msg=field_text(jst_summary.get("msg")),
        )
        tracer.record(
            "jst_upload",
            True,
            request=request_summary,
            response=jst_summary,
            push_state_saved=True,
        )

    result.jst_response = jst_summary
    success_update = _make_feishu_status_update(
        record_id,
        parent_fields,
        status=settings.sync_status_success,
        settings=settings,
        feishu=feishu,
        table_id=table_id,
    )
    try:
        feishu.batch_update_records(table_id, [success_update])
        push_state.mark_feishu_synced(apply_no)
        tracer.record(
            "feishu_update",
            True,
            update=success_update,
        )
    except Exception as e:
        err = str(e)
        result.message = (
            f"聚水潭已推送成功，但飞书状态回写失败：{err}。"
            "请再次点击推送，将仅重试飞书回写。"
        )
        result.errors.append(err)
        tracer.record("feishu_update", False, update=success_update, error=err)
        result.steps = tracer.steps
        return result

    result.ok = True
    if result.skipped_jst:
        result.message = f"订单 {apply_no} 飞书状态已补写成功（聚水潭此前已推送）"
    else:
        result.message = f"订单 {apply_no} 推送聚水潭成功"
    result.steps = tracer.steps
    logger.info("单行推送结束 request_id=%s %s", request_id, result.message)
    return result
