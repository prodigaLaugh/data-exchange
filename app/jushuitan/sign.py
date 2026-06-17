from __future__ import annotations

import hashlib
import json
import random
import string
from typing import Any


def jst_sign(params: dict[str, Any], app_secret: str) -> str:
    """
    聚水潭签名：app_secret + 按 key 字典序拼接 key+value（排除 sign），MD5 小写。
    """
    parts = [app_secret]
    for key in sorted(params.keys()):
        if key == "sign":
            continue
        parts.append(str(key))
        parts.append(str(params[key]))
    raw = "".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def random_code(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def biz_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
