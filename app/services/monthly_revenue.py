from __future__ import annotations

import calendar
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.feishu.client import FeishuClient
from app.fields import (
    COL_BARCODE,
    COL_ECOMMERCE_PLATFORM,
    COL_PRODUCT_NAME,
    COL_SALE_AMOUNT,
    COL_SALE_MONTH,
    COL_SALE_QTY,
    extract_ecommerce_platform,
    field_number,
    field_text,
)
from app.jushuitan.client import JushuitanClient

logger = logging.getLogger(__name__)

# 串行查询间隔，降低聚水潭 code=199 限频概率
CHUNK_DELAY_SECONDS = 2.0
PAGE_DELAY_SECONDS = 1.0


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


def _order_dedup_key(order: dict[str, Any]) -> str:
    return field_text(order.get("so_id")) or field_text(order.get("o_id"))


# 聚合维度：销售月份 + 电商平台(shop_name) + 商品
_GroupKey = tuple[str, str, str, str]


def _merge_orders_into_groups(
    groups: dict[_GroupKey, dict[str, Any]],
    orders: list[dict[str, Any]],
    month_label: str,
    seen_orders: set[str],
    *,
    shop_name_map: dict[str, str] | None = None,
) -> int:
    """将本批订单商品明细累加到 groups，返回本批新增订单数。"""
    new_orders = 0
    for order in orders:
        dedup_key = _order_dedup_key(order)
        if dedup_key:
            if dedup_key in seen_orders:
                continue
            seen_orders.add(dedup_key)
            new_orders += 1

        shop_name = extract_ecommerce_platform(order, shop_name_map)
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
            key = (month_label, shop_name, name, barcode)
            if key not in groups:
                groups[key] = {
                    COL_SALE_MONTH: month_label,
                    COL_ECOMMERCE_PLATFORM: shop_name,
                    COL_PRODUCT_NAME: name,
                    COL_BARCODE: barcode,
                    COL_SALE_QTY: 0.0,
                    COL_SALE_AMOUNT: 0.0,
                }
            groups[key][COL_SALE_QTY] += qty
            groups[key][COL_SALE_AMOUNT] += amount

    return new_orders


def _groups_to_rows(groups: dict[_GroupKey, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for g in groups.values():
        rows.append(
            {
                COL_SALE_MONTH: g[COL_SALE_MONTH],
                COL_ECOMMERCE_PLATFORM: g[COL_ECOMMERCE_PLATFORM],
                COL_PRODUCT_NAME: g[COL_PRODUCT_NAME],
                COL_BARCODE: g[COL_BARCODE],
                COL_SALE_QTY: g[COL_SALE_QTY],
                COL_SALE_AMOUNT: round(g[COL_SALE_AMOUNT], 2),
            }
        )
    return rows


def _fetch_and_aggregate_month_serial(
    jst: JushuitanClient,
    begin: str,
    end: str,
    month_label: str,
    *,
    request_id: str,
    shop_name_map: dict[str, str] | None = None,
) -> tuple[int, dict[_GroupKey, dict[str, Any]]]:
    """
    按时间分片串行查询聚水潭，每返回一页/一段即累加聚合，全部完成后再写飞书。
    """
    windows = _split_query_windows(begin, end)
    groups: dict[_GroupKey, dict[str, Any]] = {}
    seen_orders: set[str] = set()
    total_orders = 0

    for chunk_idx, (win_begin, win_end) in enumerate(windows, start=1):
        logger.info(
            "聚水潭订单分片开始 request_id=%s chunk=%s/%s range=%s ~ %s",
            request_id,
            chunk_idx,
            len(windows),
            win_begin,
            win_end,
        )
        page = 1
        while True:
            orders, has_next = jst.query_orders(
                modified_begin=win_begin,
                modified_end=win_end,
                page_index=page,
                page_size=50,
            )
            if page == 1 and chunk_idx == 1 and orders:
                sample = orders[0]
                logger.info(
                    "聚水潭订单店铺字段样例 request_id=%s so_id=%s shop_name=%r shop_id=%r "
                    "shop_site=%r order_from=%r resolved=%r",
                    request_id,
                    sample.get("so_id"),
                    sample.get("shop_name"),
                    sample.get("shop_id"),
                    sample.get("shop_site"),
                    sample.get("order_from"),
                    extract_ecommerce_platform(sample, shop_name_map),
                )
            added = _merge_orders_into_groups(
                groups,
                orders,
                month_label,
                seen_orders,
                shop_name_map=shop_name_map,
            )
            total_orders += added
            logger.info(
                "聚水潭订单分页聚合 request_id=%s chunk=%s/%s page=%s "
                "batch_orders=%s new_orders=%s product_groups=%s",
                request_id,
                chunk_idx,
                len(windows),
                page,
                len(orders),
                added,
                len(groups),
            )
            if not has_next or not orders:
                break
            page += 1
            time.sleep(PAGE_DELAY_SECONDS)

        if chunk_idx < len(windows):
            time.sleep(CHUNK_DELAY_SECONDS)

    return total_orders, groups


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
        shop_name_map = jst.query_shops_map()
    except Exception as e:
        logger.warning("加载聚水潭店铺映射失败，将仅使用订单内店铺字段 request_id=%s err=%s", request_id, e)
        shop_name_map = {}

    try:
        total_orders, groups = _fetch_and_aggregate_month_serial(
            jst,
            begin,
            end,
            month_label,
            request_id=request_id,
            shop_name_map=shop_name_map,
        )
    except Exception as e:
        logger.exception("聚水潭订单查询失败 request_id=%s", request_id)
        result.errors.append(str(e))
        result.message = f"订单查询失败: {e}"
        return result

    result.orders_fetched = total_orders
    rows = _groups_to_rows(groups)
    result.product_groups = len(rows)

    empty_platforms = sum(1 for r in rows if not r.get(COL_ECOMMERCE_PLATFORM))
    if empty_platforms:
        logger.warning(
            "汇总行中有 %s/%s 条缺少电商平台 request_id=%s",
            empty_platforms,
            len(rows),
            request_id,
        )
    if rows:
        logger.info("写入飞书样例行 request_id=%s sample=%s", request_id, rows[0])

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
