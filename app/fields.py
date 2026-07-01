from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 文创营收登记（推送源表）列名
COL_ORDER_NO = "订单编号"
COL_SYNC_STATUS = "同步状态"
COL_SYNC_TIME = "同步时间"
COL_FAIL_REASON = "失败原因"
COL_TRACKING_NO = "快递单号"
COL_CHANNEL = "渠道编码"
COL_ORDER_DATE = "下单日期"
COL_ADDRESS = "收货地址"
COL_TOTAL_AMOUNT = "合计金额"
COL_FREIGHT = "快递费"
COL_BARCODE = "69码"
COL_RETAIL_PRICE = "零售价"
COL_DISCOUNT_PRICE = "折扣价"
COL_QTY = "数量"
COL_PRODUCT_NAME = "商品名称"

# 电商营收登记（月度汇总目标表）列名
COL_SALE_MONTH = "销售日期"
COL_ECOMMERCE_PLATFORM = "电商平台"
COL_SALE_QTY = "销量"
COL_SALE_AMOUNT = "金额"


def field_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("text") or first.get("name") or "").strip()
        return str(first).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or value.get("name") or "").strip()
    return str(value).strip()


def field_number(value: Any) -> float:
    text = field_text(value).replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def field_datetime_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = field_text(value)
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def format_order_date(value: Any) -> str:
    ms = field_datetime_ms(value)
    if ms:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    text = field_text(value)
    return text or datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_address(value: Any) -> tuple[str, str, str]:
    text = field_text(value)
    if not text:
        return "", "", ""
    parts = text.split()
    if len(parts) >= 3:
        return parts[0], parts[1], " ".join(parts[2:])
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return text, "", ""


def now_sync_time_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def now_sync_time_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def feishu_sync_time_value(existing: Any, *, use_ms: bool) -> int | str:
    """
    写入飞书「同步时间」列。
    - 日期字段(use_ms=True)：Unix 毫秒时间戳
    - 文本字段(use_ms=False)：YYYY-MM-DD HH:mm:ss 字符串
    若该行已有值，按已有值的类型自动对齐。
    """
    if isinstance(existing, (int, float)) and existing:
        return now_sync_time_ms()
    text = field_text(existing)
    if text and not text.replace(".", "", 1).isdigit():
        return now_sync_time_str()
    return now_sync_time_ms() if use_ms else now_sync_time_str()


def extract_ecommerce_platform(
    order: dict[str, Any],
    shop_name_map: dict[str, str] | None = None,
) -> str:
    """
    从聚水潭订单解析电商平台名称。
    订单查询接口部分场景不返回 shop_name，需回退 shop_site / order_from / shop_id 映射。
    """
    for key in ("shop_name", "shopName"):
        name = field_text(order.get(key))
        if name:
            return name

    for key in ("shop_site", "order_from"):
        val = field_text(order.get(key))
        if val:
            return val

    shop_id = field_text(order.get("shop_id"))
    if shop_id and shop_name_map:
        mapped = shop_name_map.get(shop_id)
        if mapped:
            return mapped

    return shop_id
