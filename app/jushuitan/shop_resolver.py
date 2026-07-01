from __future__ import annotations

from typing import Any

from app.fields import field_text


def build_shop_id_resolver(shops: list[dict[str, Any]]):
    """
    将飞书「渠道编码」解析为聚水潭 upload 接口所需的数字 shop_id。
    文档：https://openweb.jushuitan.com/dev-doc?docType=4&docId=18
    """
    lookup: dict[str, int] = {}

    for shop in shops:
        if not isinstance(shop, dict):
            continue
        raw_id = shop.get("shop_id")
        if raw_id is None:
            continue
        try:
            sid = int(raw_id)
        except (TypeError, ValueError):
            continue
        lookup[str(sid)] = sid
        for key in ("shop_name", "nick", "short_name", "group_name"):
            val = field_text(shop.get(key))
            if val:
                lookup[val] = sid
                lookup[val.lower()] = sid

    def resolve(channel: str) -> int | None:
        ch = field_text(channel)
        if not ch:
            return None
        if ch.isdigit():
            return int(ch)
        if ch in lookup:
            return lookup[ch]
        lower = ch.lower()
        if lower in lookup:
            return lookup[lower]
        return None

    return resolve
