from __future__ import annotations

import calendar
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.feishu.client import FeishuClient
from app.fields import COL_BARCODE, COL_PRODUCT_NAME, COL_SALE_AMOUNT, COL_SALE_MONTH, COL_SALE_QTY, field_number, field_text
from app.jushuitan.client import JushuitanClient

logger = logging.getLogger(__name__)


@dataclass
class MonthlyRevenueResult:
    request_id: str
    target_month: str
    orders_fetched: int
    rows_appended: int
    product_groups: int
    message: str = ""
    errors: list[str] = field(default_factory=list)


def _previous_month_range(now: datetime | None = None) -> tuple[str, str, str]:
    """返回 (YYYY-MM, modified_begin, modified_end)。"""
    now = now or datetime.now()
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this_month - timedelta(seconds=1)
    year, month = last_prev.year, last_prev.month
    month_label = f"{year:04d}-{month:02d}"
    last_day = calendar.monthrange(year, month)[1]
    begin = f"{year:04d}-{month:02d}-01 00:00:00"
    end = f"{year:04d}-{month:02d}-{last_day:02d} 23:59:59"
    return month_label, begin, end


_QUERY_TIME_FMT = "%Y-%m-%d %H:%M:%S"
# 聚水潭订单查询：modified_begin/modified_end 间隔不得超过 7 天
# https://openweb.jushuitan.com/dev-doc?docType=4&docId=22
_MAX_QUERY_DAYS = 7


def _split_query_windows(begin: str, end: str, max_days: int = _MAX_QUERY_DAYS) -> list[tuple[str, str]]:
    """将月份时间范围拆成若干段，每段不超过 max_days 天。"""
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


def _dedupe_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按线上单号去重，避免分片查询边界重复。"""
    unique: dict[str, dict[str, Any]] = {}
    for order in orders:
        key = field_text(order.get("so_id")) or field_text(order.get("o_id"))
        if not key:
            unique[str(id(order))] = order
        else:
            unique[key] = order
    return list(unique.values())


def _fetch_orders_for_month(
    jst: JushuitanClient,
    begin: str,
    end: str,
    *,
    request_id: str,
) -> list[dict[str, Any]]:
    windows = _split_query_windows(begin, end)
    all_orders: list[dict[str, Any]] = []
    for i, (win_begin, win_end) in enumerate(windows, start=1):
        logger.info(
            "聚水潭订单分片查询 request_id=%s chunk=%s/%s range=%s ~ %s",
            request_id,
            i,
            len(windows),
            win_begin,
            win_end,
        )
        chunk = jst.query_orders_all(modified_begin=win_begin, modified_end=win_end)
        all_orders.extend(chunk)
    return _dedupe_orders(all_orders)


def _aggregate_orders(orders: list[dict[str, Any]], month_label: str) -> list[dict[str, Any]]:
    """
    按 (销售月份, 商品名称, 69码) 汇总销量与金额。
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for order in orders:
        items = order.get("items") or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = field_text(item.get("name"))
            barcode = field_text(item.get("sku_id") or item.get("shop_sku_id"))
            qty = field_number(item.get("qty"))
            amount = field_number(item.get("amount"))
            if not name and not barcode:
                continue
            key = (month_label, name, barcode)
            if key not in groups:
                groups[key] = {
                    COL_SALE_MONTH: month_label,
                    COL_PRODUCT_NAME: name,
                    COL_BARCODE: barcode,
                    COL_SALE_QTY: 0.0,
                    COL_SALE_AMOUNT: 0.0,
                }
            groups[key][COL_SALE_QTY] += qty
            groups[key][COL_SALE_AMOUNT] += amount

    rows: list[dict[str, Any]] = []
    for g in groups.values():
        rows.append(
            {
                COL_SALE_MONTH: g[COL_SALE_MONTH],
                COL_PRODUCT_NAME: g[COL_PRODUCT_NAME],
                COL_BARCODE: g[COL_BARCODE],
                COL_SALE_QTY: g[COL_SALE_QTY],
                COL_SALE_AMOUNT: round(g[COL_SALE_AMOUNT], 2),
            }
        )
    return rows


def run_monthly_revenue_sync(
    *,
    feishu: FeishuClient,
    jst: JushuitanClient,
    settings: Settings,
    now: datetime | None = None,
) -> MonthlyRevenueResult:
    request_id = str(uuid.uuid4())
    month_label, begin, end = _previous_month_range(now)
    result = MonthlyRevenueResult(
        request_id=request_id,
        target_month=month_label,
        orders_fetched=0,
        rows_appended=0,
        product_groups=0,
    )

    logger.info(
        "开始月度营收汇总 request_id=%s month=%s range=%s ~ %s",
        request_id,
        month_label,
        begin,
        end,
    )

    try:
        orders = _fetch_orders_for_month(jst, begin, end, request_id=request_id)
    except Exception as e:
        logger.exception("聚水潭订单查询失败 request_id=%s", request_id)
        result.errors.append(str(e))
        result.message = f"订单查询失败: {e}"
        return result

    result.orders_fetched = len(orders)
    rows = _aggregate_orders(orders, month_label)
    result.product_groups = len(rows)

    if rows:
        try:
            result.rows_appended = feishu.batch_create_records(
                settings.feishu_table_dianshang,
                rows,
            )
        except Exception as e:
            logger.exception("写入电商营收登记失败 request_id=%s", request_id)
            result.errors.append(str(e))
            result.message = f"汇总 {len(rows)} 条但写入飞书失败: {e}"
            return result

    result.message = (
        f"{month_label} 汇总完成：查询订单 {result.orders_fetched} 条，"
        f"汇总产品 {result.product_groups} 条，追加写入 {result.rows_appended} 行"
    )
    logger.info("月度营收汇总结束 request_id=%s %s", request_id, result.message)
    return result
