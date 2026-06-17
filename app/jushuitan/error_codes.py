"""
聚水潭开放平台错误码与 token 判断。

参考文档：https://openweb.jushuitan.com/doc?docId=320
（同 https://open.jushuitan.com/document/2021.html）
"""

from __future__ import annotations

from typing import Any

# access_token 超时，应 refresh / getInitToken 后重试
JST_TOKEN_REFRESH_CODES: frozenset[int] = frozenset({100})

# 时间戳误差过大，可用新 timestamp 重试一次
JST_TIMESTAMP_RETRY_CODES: frozenset[int] = frozenset({180})

# 常见错误码排查建议（摘自官方文档）
JST_ERROR_HINTS: dict[int, str] = {
    0: "执行成功",
    100: "合作者授权参数有误或 Token 超时，将自动刷新 token 后重试",
    110: "IP 不在白名单，请联系聚水潭管理员添加服务器出口 IP",
    120: "签名错误，请检查 app_secret 与签名算法",
    130: "传输数据不能为空",
    140: "参数不符合规范，请检查 biz 业务参数",
    150: "内部处理异常",
    160: "内部保存失败，请检查业务数据",
    170: "店铺编号不存在，请检查表格中的渠道编码",
    180: "请求时间戳 TS 无效（与服务端误差需在 10 分钟内），将自动重试",
    190: "接口无权限，请按文档申请接口权限",
    199: "调用太频繁，请稍后再试",
    200: "调用频次超过限制，请降低并发",
}


def _normalize_code(code: Any) -> int | None:
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def is_token_refresh_error(code: Any, msg: str = "") -> bool:
    """是否应刷新 access_token 后重试。"""
    c = _normalize_code(code)
    if c is not None and c in JST_TOKEN_REFRESH_CODES:
        return True
    lower = (msg or "").lower()
    return any(
        k in lower
        for k in (
            "token超时",
            "token 超时",
            "token过期",
            "token失效",
            "access_token",
            "token invalid",
            "token expired",
            "无效的token",
            "无效token",
        )
    )


def is_timestamp_retry_error(code: Any) -> bool:
    """是否因时间戳问题可立即重试。"""
    c = _normalize_code(code)
    return c is not None and c in JST_TIMESTAMP_RETRY_CODES


def format_jst_error(code: Any, msg: str) -> str:
    c = _normalize_code(code)
    hint = JST_ERROR_HINTS.get(c) if c is not None else None
    base = f"聚水潭 API 失败(code={code}): {msg}"
    return f"{base}；{hint}" if hint else base
