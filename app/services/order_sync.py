from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.feishu.client import FeishuClient
from app.fields import (
    COL_DS_AMOUNT,
    COL_DS_ITEM_NAME,
    COL_DS_LOGISTICS_COMPANY,
    COL_DS_ORDER_DATE,
    COL_DS_OUTER_PAY_ID,
    COL_DS_PAY_DATE,
    COL_DS_PAYMENT,
    COL_DS_PRICE,
    COL_DS_QTY,
    COL_DS_RECEIVER_NAME,
    COL_DS_SHOP_NAME,
    COL_DS_SHOP_SITE,
    COL_DS_SKU_ID,
    COL_DS_SO_ID,
    COL_DS_STANDARD_NAME,
    COL_DS_STATUS,
    COL_DS_TRACKING_NO,
    field_int,
    field_money,
    field_text,
    format_order_date,
)
from app.jushuitan.client import JushuitanClient
from app.order_line_index import OrderLineIndexStore, line_index_key

logger = logging.getLogger(__name__)

_QUERY_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_MAX_QUERY_DAYS = 7
CHUNK_DELAY_SECONDS = 2.0
PAGE_DELAY_SECONDS = 1.0
SKU_QUERY_DELAY_SECONDS = 1.0
WEEKLY_LOOKBACK_DAYS = 14


@dataclass
class OrderSyncResult:
    request_id: str
    table_id: str
    modified_begin: str
    modified_end: str
    orders_fetched: int
    rows_built: int
    rows_created: int
    rows_updated: int
    message: str = ""
    errors: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)


class _OrderSyncTracer:
    def __init__(self, request_id: str, table_id: str) -> None:
        self.request_id = request_id
        self.table_id = table_id
        self.steps: list[dict[str, Any]] = []

    def record(self, step: str, ok: bool, **detail: Any) -> None:
        entry: dict[str, Any] = {"step": step, "ok": ok, **detail}
        self.steps.append(entry)
        logger.info(
            "order_sync step request_id=%s table_id=%s step=%s ok=%s detail=%s",
            self.request_id,
            self.table_id,
            step,
            ok,
            detail,
        )


def _split_query_windows(begin: str, end: str, max_days: int = _MAX_QUERY_DAYS) -> list[tuple[str, str]]:
    start = datetime.strptime(begin, _QUERY_TIME_FMT)
    finish = datetime.strptime(end, _QUERY_TIME_FMT)
    windows: list[tuple[str, str]] = []
    cursor = start
    while cursor <= finish:
        window_end = min(
            cursor + timedelta(days=max_days - 1, hours=23, minutes=59, seconds=59),
            finish,
        )
        windows.append((cursor.strftime(_QUERY_TIME_FMT), window_end.strftime(_QUERY_TIME_FMT)))
        cursor = window_end + timedelta(seconds=1)
    return windows


def last_n_days_range(*, days: int = WEEKLY_LOOKBACK_DAYS, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now()
    end = now.replace(microsecond=0)
    begin = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return begin.strftime(_QUERY_TIME_FMT), end.strftime(_QUERY_TIME_FMT)


def year_range(year: int) -> tuple[str, str]:
    return f"{year:04d}-01-01 00:00:00", f"{year:04d}-12-31 23:59:59"


def _first_pay(order: dict[str, Any]) -> dict[str, Any]:
    pays = order.get("pays")
    if isinstance(pays, list):
        for p in pays:
            if isinstance(p, dict):
                return p
    if isinstance(pays, dict):
        return pays
    return {}


def _order_status(order: dict[str, Any]) -> str:
    for key in ("status", "shop_status"):
        text = field_text(order.get(key))
        if text:
            return text
    return ""


def _order_tracking_no(order: dict[str, Any]) -> str:
    l_id = order.get("l_id")
    if isinstance(l_id, list):
        parts = [field_text(x) for x in l_id if field_text(x)]
        return "、".join(parts)
    text = field_text(l_id) or field_text(order.get("logistics_no"))
    return text


def _flatten_order_to_rows(order: dict[str, Any]) -> list[dict[str, Any]]:
    so_id = field_text(order.get("so_id"))
    if not so_id:
        return []

    pay = _first_pay(order)
    pay_date = pay.get("pay_date") or order.get("pay_date")
    order_header: dict[str, Any] = {
        COL_DS_SO_ID: so_id,
        COL_DS_SHOP_NAME: field_text(order.get("shop_name")),
        COL_DS_SHOP_SITE: field_text(order.get("shop_site")),
        COL_DS_ORDER_DATE: format_order_date(order.get("order_date")),
        COL_DS_PAY_DATE: format_order_date(pay_date) if pay_date else "",
        COL_DS_STATUS: _order_status(order),
        COL_DS_LOGISTICS_COMPANY: field_text(order.get("logistics_company")),
        COL_DS_TRACKING_NO: _order_tracking_no(order),
        COL_DS_RECEIVER_NAME: field_text(order.get("receiver_name")),
        COL_DS_OUTER_PAY_ID: field_text(pay.get("outer_pay_id")),
        COL_DS_PAYMENT: field_text(pay.get("payment")),
    }

    items = order.get("items") or []
    if not isinstance(items, list) or not items:
        return []

    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sku_id = field_text(item.get("sku_id"))
        if not sku_id:
            continue
        row = dict(order_header)
        row[COL_DS_SKU_ID] = sku_id
        row[COL_DS_ITEM_NAME] = field_text(item.get("name"))
        row[COL_DS_QTY] = field_int(item.get("qty"))
        row[COL_DS_PRICE] = field_money(item.get("price"), decimals=4)
        row[COL_DS_AMOUNT] = field_money(item.get("amount"))
        rows.append(row)
    return rows


def _bootstrap_index_from_feishu(
    feishu: FeishuClient,
    table_id: str,
    index: OrderLineIndexStore,
) -> int:
    records = feishu.list_all_records(table_id)
    mapping: dict[str, str] = {}
    for rec in records:
        rid = rec.get("record_id")
        fields = rec.get("fields") or {}
        if not isinstance(fields, dict) or not rid:
            continue
        so_id = field_text(fields.get(COL_DS_SO_ID))
        sku_id = field_text(fields.get(COL_DS_SKU_ID))
        if so_id and sku_id:
            mapping[line_index_key(so_id, sku_id)] = str(rid)
    index.set_many(mapping)
    return len(mapping)


def _fetch_orders_serial(
    jst: JushuitanClient,
    begin: str,
    end: str,
    *,
    request_id: str,
    tracer: _OrderSyncTracer,
) -> list[dict[str, Any]]:
    windows = _split_query_windows(begin, end)
    all_orders: list[dict[str, Any]] = []
    seen_so: set[str] = set()

    for chunk_idx, (win_begin, win_end) in enumerate(windows, start=1):
        logger.info(
            "聚水潭订单分片 request_id=%s chunk=%s/%s %s ~ %s",
            request_id,
            chunk_idx,
            len(windows),
            win_begin,
            win_end,
        )
        page = 1
        chunk_orders = 0
        while True:
            orders, has_next = jst.query_orders(
                modified_begin=win_begin,
                modified_end=win_end,
                page_index=page,
                page_size=50,
            )
            for order in orders:
                so_id = field_text(order.get("so_id"))
                if so_id and so_id not in seen_so:
                    seen_so.add(so_id)
                    all_orders.append(order)
                    chunk_orders += 1
                elif so_id:
                    # 同一 so_id 在重叠窗口可能出现，保留最新一条（后写入覆盖）
                    for i, existing in enumerate(all_orders):
                        if field_text(existing.get("so_id")) == so_id:
                            all_orders[i] = order
                            break
            if not has_next or not orders:
                break
            page += 1
            time.sleep(PAGE_DELAY_SECONDS)

        tracer.record(
            "jst_orders",
            True,
            chunk=chunk_idx,
            range_begin=win_begin,
            range_end=win_end,
            new_orders=chunk_orders,
            total_unique=len(all_orders),
        )
        if chunk_idx < len(windows):
            time.sleep(CHUNK_DELAY_SECONDS)

    return all_orders


def _attach_standard_names(
    rows: list[dict[str, Any]],
    sku_map: dict[str, str],
) -> None:
    for row in rows:
        sku_id = field_text(row.get(COL_DS_SKU_ID))
        if sku_id and sku_id in sku_map:
            row[COL_DS_STANDARD_NAME] = sku_map[sku_id]


def _upsert_rows(
    feishu: FeishuClient,
    table_id: str,
    rows: list[dict[str, Any]],
    index: OrderLineIndexStore,
) -> tuple[int, int, list[dict[str, str]]]:
    updates: list[dict[str, Any]] = []
    creates: list[dict[str, Any]] = []
    new_index: list[dict[str, str]] = []

    for row in rows:
        so_id = field_text(row.get(COL_DS_SO_ID))
        sku_id = field_text(row.get(COL_DS_SKU_ID))
        if not so_id or not sku_id:
            continue
        record_id = index.get(so_id, sku_id)
        if record_id:
            updates.append({"record_id": record_id, "fields": row})
        else:
            creates.append(row)

    created = 0
    updated = 0

    if updates:
        updated = feishu.batch_update_records(table_id, updates)

    if creates:
        created_before = len(creates)
        created = feishu.batch_create_records(table_id, creates)
        # batch_create 不返回 record_id，需回查索引：用本次写入的 key 在下次从飞书 bootstrap
        # 创建后立即 list 成本高；改为创建后按 so_id+sku_id 批量补索引
        if created > 0 and created_before > 0:
            _refresh_index_for_rows(feishu, table_id, creates, index)

    return created, updated, new_index


def _refresh_index_for_rows(
    feishu: FeishuClient,
    table_id: str,
    rows: list[dict[str, Any]],
    index: OrderLineIndexStore,
) -> None:
    """创建后从飞书表刷新新行的 record_id（按 so_id+sku_id 匹配）。"""
    targets = {
        line_index_key(field_text(r.get(COL_DS_SO_ID)), field_text(r.get(COL_DS_SKU_ID)))
        for r in rows
        if field_text(r.get(COL_DS_SO_ID)) and field_text(r.get(COL_DS_SKU_ID))
    }
    if not targets:
        return
    records = feishu.list_all_records(table_id)
    mapping: dict[str, str] = {}
    for rec in records:
        rid = rec.get("record_id")
        fields = rec.get("fields") or {}
        if not isinstance(fields, dict) or not rid:
            continue
        key = line_index_key(
            field_text(fields.get(COL_DS_SO_ID)),
            field_text(fields.get(COL_DS_SKU_ID)),
        )
        if key in targets:
            mapping[key] = str(rid)
    index.set_many(mapping)


def run_order_sync(
    *,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
    index_store: OrderLineIndexStore,
    modified_begin: str,
    modified_end: str,
    table_id: str | None = None,
    request_id: str | None = None,
) -> OrderSyncResult:
    """
    聚水潭订单明细同步到飞书电商表：按 so_id + sku_id upsert。
    使用 modified_begin/end 查询（间隔拆分为 ≤7 天）。
    """
    request_id = request_id or str(uuid.uuid4())
    table_id = table_id or settings.feishu_table_dianshang
    tracer = _OrderSyncTracer(request_id, table_id)
    result = OrderSyncResult(
        request_id=request_id,
        table_id=table_id,
        modified_begin=modified_begin,
        modified_end=modified_end,
        orders_fetched=0,
        rows_built=0,
        rows_created=0,
        rows_updated=0,
    )

    logger.info(
        "开始订单明细同步 request_id=%s table=%s range=%s ~ %s",
        request_id,
        table_id,
        modified_begin,
        modified_end,
    )
    tracer.record("start", True, modified_begin=modified_begin, modified_end=modified_end)

    if index_store.is_empty():
        bootstrapped = _bootstrap_index_from_feishu(feishu, table_id, index_store)
        tracer.record("index_bootstrap", True, count=bootstrapped)

    try:
        orders = _fetch_orders_serial(
            jst, modified_begin, modified_end, request_id=request_id, tracer=tracer
        )
    except Exception as e:
        err = str(e)
        result.errors.append(err)
        result.message = f"订单查询失败: {err}"
        tracer.record("jst_orders", False, error=err)
        result.steps = tracer.steps
        return result

    result.orders_fetched = len(orders)

    rows: list[dict[str, Any]] = []
    for order in orders:
        rows.extend(_flatten_order_to_rows(order))
    result.rows_built = len(rows)

    if not rows:
        result.message = "时间范围内无有效订单商品行"
        tracer.record("flatten", True, rows=0)
        result.steps = tracer.steps
        return result

    tracer.record("flatten", True, rows=len(rows))

    sku_ids = [field_text(r.get(COL_DS_SKU_ID)) for r in rows]
    try:
        sku_map = jst.query_skus([s for s in sku_ids if s])
        time.sleep(SKU_QUERY_DELAY_SECONDS)
        _attach_standard_names(rows, sku_map)
        tracer.record("jst_skus", True, sku_count=len(sku_map), requested=len(set(sku_ids)))
    except Exception as e:
        err = str(e)
        logger.warning("SKU 标准品名查询失败 request_id=%s err=%s", request_id, err)
        result.errors.append(f"标准品名查询失败: {err}")
        tracer.record("jst_skus", False, error=err)

    try:
        created, updated, _ = _upsert_rows(feishu, table_id, rows, index_store)
        result.rows_created = created
        result.rows_updated = updated
        tracer.record(
            "feishu_upsert",
            True,
            created=created,
            updated=updated,
        )
    except Exception as e:
        err = str(e)
        result.errors.append(err)
        result.message = f"飞书写入失败: {err}"
        tracer.record("feishu_upsert", False, error=err)
        result.steps = tracer.steps
        return result

    result.steps = tracer.steps
    result.message = (
        f"订单同步完成：查询 {result.orders_fetched} 单，"
        f"明细 {result.rows_built} 行，新增 {result.rows_created}，更新 {result.rows_updated}"
    )
    logger.info("订单明细同步结束 request_id=%s %s", request_id, result.message)
    return result


def run_weekly_order_sync(
    *,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
    index_store: OrderLineIndexStore,
    now: datetime | None = None,
) -> OrderSyncResult:
    begin, end = last_n_days_range(days=WEEKLY_LOOKBACK_DAYS, now=now)
    return run_order_sync(
        feishu=feishu,
        jst=jst,
        settings=settings,
        index_store=index_store,
        modified_begin=begin,
        modified_end=end,
    )
