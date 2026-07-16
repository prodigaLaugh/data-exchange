from __future__ import annotations

import ast
import json
import re
from datetime import datetime, timezone
from typing import Any

# 文创营收登记（推送源表）列名 — 旧批量推送 schema
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
COL_REMARK = "备注"

# 文创营收登记 — 单行推送 schema（父表）
COL_APPLY_NO = "申请编号"
COL_APPLY_DATE = "申请日期"
COL_INVOICE_AMOUNT = "开票金额"
COL_EXPRESS_ADDRESS = "快递地址"
COL_LINKED_PRODUCTS = "关联文创营收"
COL_SYNC_REASON = "同步原因"

# 关联子表（品的数据）列名
COL_SUB_ORDER_NO = "订单编号"

# 电商营收登记（订单明细同步目标表）列名
COL_DS_SO_ID = "线上订单号"
COL_DS_SHOP_NAME = "店铺名称"
COL_DS_SHOP_SITE = "平台站点"
COL_DS_ORDER_DATE = "下单时间"
COL_DS_PAY_DATE = "付款日期"
COL_DS_STATUS = "状态"
COL_DS_LOGISTICS_COMPANY = "快递公司"
COL_DS_TRACKING_NO = "快递单号"
COL_DS_RECEIVER_NAME = "收货人"
COL_DS_OUTER_PAY_ID = "支付单号"
COL_DS_PAYMENT = "付款方式"
COL_DS_ITEM_NAME = "商品名称"
COL_DS_QTY = "数量"
COL_DS_PRICE = "商品单价"
COL_DS_AMOUNT = "商品金额"
COL_DS_STANDARD_NAME = "标准品名"
COL_DS_SKU_ID = "69码"

# 已废弃：旧月度汇总列名（保留常量避免误引用）
COL_SALE_MONTH = "销售日期"
COL_ECOMMERCE_PLATFORM = "电商平台"
COL_SALE_QTY = "销量"
COL_SALE_AMOUNT = "金额"


def _regex_extract_rich_text(text: str) -> list[str]:
    """从无法 literal_eval 的富文本字符串里提取 text 字段值。"""
    return re.findall(r"""['"]text['"]\s*:\s*['"]((?:\\.|[^'\\"])*)['"]""", text)


def _parse_rich_text_literal(text: str) -> Any | None:
    """解析被序列化成字符串的飞书富文本字段，如 \"[{'text': '...', 'type': 'text'}]\"。 """
    s = text.strip()
    if not s.startswith("["):
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return None


def _rich_text_segments(value: Any) -> list[str]:
    """从飞书富文本/文本字段提取纯文本片段。"""
    if value is None:
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_rich_text_segments(item))
        return parts
    if isinstance(value, dict):
        text = value.get("text")
        if text is None:
            text = value.get("name") or value.get("value")
        if text is None:
            return []
        if isinstance(text, (list, dict)):
            return _rich_text_segments(text)
        s = str(text).strip()
        return [s] if s else []
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        parsed = _parse_rich_text_literal(s)
        if parsed is not None:
            return _rich_text_segments(parsed)
        regex_parts = _regex_extract_rich_text(s)
        if regex_parts:
            return regex_parts
        return [s]
    if isinstance(value, (int, float)):
        return [str(value)]
    return [str(value).strip()]


def field_text(value: Any) -> str:
    return "".join(_rich_text_segments(value)).strip()


def field_number(value: Any) -> float:
    text = field_text(value).replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def field_int(value: Any) -> int:
    return int(round(field_number(value)))


def field_money(value: Any, *, decimals: int = 2) -> float:
    n = field_number(value)
    return round(n, decimals)


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


def row_so_id(fields: dict[str, Any]) -> str:
    """父表订单号：优先申请编号，兼容旧字段订单编号。"""
    return field_text(fields.get(COL_APPLY_NO)) or field_text(fields.get(COL_ORDER_NO))


def row_apply_date(fields: dict[str, Any]) -> Any:
    """申请日期，兼容旧列名「申请时间」。"""
    for key in (COL_APPLY_DATE, "申请时间"):
        if key in fields and field_text(fields.get(key)):
            return fields.get(key)
    return None


def extract_link_record_ids(value: Any) -> list[str]:
    """从飞书关联字段解析 link_record_ids。

    飞书 GET record 常见格式：
    - 字符串列表：["recXXX", "recYYY"]
    - 对象含 record_ids：{"record_ids": ["recXXX"], "table_id": "..."}
    - 上述对象的列表：[{"record_ids": [...], "type": "text", ...}]
    """
    if value is None:
        return []

    def _ids_from_dict(obj: dict[str, Any]) -> list[str]:
        for key in ("link_record_ids", "record_ids", "recordIds"):
            link_ids = obj.get(key)
            if isinstance(link_ids, list):
                return [str(x) for x in link_ids if x]
        rid = obj.get("record_id") or obj.get("id")
        return [str(rid)] if rid else []

    if isinstance(value, dict):
        return _ids_from_dict(value)

    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str) and item:
                result.append(item)
            elif isinstance(item, dict):
                result.extend(_ids_from_dict(item))
        return result

    return []


def parse_express_address(value: Any) -> tuple[str, str, str]:
    """
    解析「快递地址」：地址 姓名 手机号 [备注]。
    多个连续空格归一为单个空格后按空格拆分；备注不参与聚水潭收货字段。
    """
    text = re.sub(r"\s+", " ", field_text(value)).strip()
    if not text:
        return "", "", ""
    parts = text.split(" ")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        phone = _extract_mobile(parts[1])
        name = parts[1].replace(phone, "").strip() if phone else parts[1]
        return parts[0], name, phone
    return text, "", _extract_mobile(text)


def parse_address(value: Any) -> tuple[str, str, str]:
    """
    解析飞书「收货地址」单元格。
    支持：
    1) 收货地址：… 收货人：… 手机号：… [备注：…]（多空格归一后按标签切分）
    2) 旧格式：地址 姓名 电话（空格分隔）
    """
    text = re.sub(r"\s+", " ", field_text(value)).strip()
    if not text:
        return "", "", ""

    label_re = re.compile(r"(收货地址|收货人|手机号|手机|备注)[:：]?\s*")
    matches = list(label_re.finditer(text))
    if matches:
        fields: dict[str, str] = {}
        for i, m in enumerate(matches):
            label = m.group(1)
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            val = text[start:end].strip()
            if label == "收货地址":
                fields["address"] = val
            elif label == "收货人":
                fields["name"] = val
            elif label in ("手机号", "手机"):
                fields["phone"] = val
        address = fields.get("address", "")
        name = fields.get("name", "")
        phone = fields.get("phone", "")
        if not phone:
            phone = _extract_mobile(name) or _extract_mobile(text)
        if name and phone and phone in name:
            name = name.replace(phone, "").strip()
        return address, name, phone

    parts = text.split(" ")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        phone = _extract_mobile(parts[1])
        name = parts[1].replace(phone, "").strip() if phone else parts[1]
        return parts[0], name, phone
    return text, "", _extract_mobile(text)


def _extract_mobile(text: str) -> str:
    m = re.search(r"1\d{10}", text or "")
    return m.group(0) if m else ""


def now_sync_time_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def now_sync_time_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_sync_time_use_ms(
    existing: Any,
    *,
    settings_use_ms: bool,
    field_is_datetime: bool | None = None,
) -> bool:
    """决定写入飞书「同步时间」时用毫秒还是文本；优先按行内已有值类型对齐。"""
    if isinstance(existing, (int, float)) and existing:
        return True
    text = field_text(existing)
    if text and not text.replace(".", "", 1).isdigit():
        return False
    if settings_use_ms:
        return True
    if field_is_datetime:
        return True
    return False


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
