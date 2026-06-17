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
        orders = jst.query_orders_all(modified_begin=begin, modified_end=end)
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
