from __future__ import annotations

from typing import Any

from app.fields import field_text


def parse_shop_id(value: Any) -> int | None:
    """飞书「渠道编码」列现为聚水潭 shop_id（数字）。"""
    text = field_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None
