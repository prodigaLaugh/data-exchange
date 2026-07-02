"""
飞书开放平台错误码与 token 判断。

参考文档：
- 通用参数: https://open.feishu.cn/document/server-docs/api-call-guide/terminology
- 服务端错误码: https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code
"""

from __future__ import annotations

from typing import Any

# tenant_access_token 失效/过期，应重新获取后重试（不中断业务流程）
FEISHU_TOKEN_REFRESH_CODES: frozenset[int] = frozenset(
    {
        4001,  # Invalid token, please refresh
        20005,  # invalid access_token
        20013,  # The tenant access token passed is invalid
        99991663,  # Invalid access token (tenant_access_token 已过期等)
        99991664,  # invalid app token
        99991665,  # invalid tenant code (tenant_access_token 非法)
        99991668,  # Invalid access token (user_access_token)
        99991671,  # Invalid token: must start with t-/u-
        99991677,  # token expire
    }
)

# 常见错误码排查建议（摘自官方文档）
FEISHU_ERROR_HINTS: dict[int, str] = {
    4001: "token 无效或已过期，请重新获取 tenant_access_token",
    10013: "获取 Tenant Token 失败，请检查 app_id / app_secret",
    10015: "App Secret 错误，请在开发者后台核对",
    20005: "access_token 无效，请重新获取 tenant_access_token",
    20013: "tenant_access_token 无效，请重新获取",
    91403: "应用无多维表格权限，请为应用添加文档权限或开通 bitable:app",
    99991400: "请求过于频繁，请降低调用频率",
    99991661: "请求 Header 缺少 Authorization: Bearer <tenant_access_token>",
    99991663: "tenant_access_token 已过期或无效，将自动重新获取",
    99991665: "tenant_access_token 非法，将自动重新获取",
    99991672: "应用未申请所需 API 权限，请在后台开通并发布",
    99991677: "token 已过期，将自动重新获取",
    1254043: "记录不存在(record_id 无效或不在当前 table_id 中)，请核对 recordId 是否为 rec 开头的行 ID",
    1254045: "字段名不存在，请核对飞书表头与代码 COL_* 常量是否一致",
    1254060: "文本/多行文本字段格式错误：文本列应写字符串，勿写数字；多行文本勿写空字符串",
    1254062: "单选字段格式错误：选项须与飞书下拉选项完全一致（如「同步成功」）",
    1254064: "日期字段格式错误：日期列应写毫秒时间戳，可设置 SYNC_TIME_USE_MS=true",
}


def _normalize_code(code: Any) -> int | None:
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def is_token_refresh_error(code: Any, msg: str = "") -> bool:
    """是否应刷新 tenant_access_token 后重试。"""
    c = _normalize_code(code)
    if c is not None and c in FEISHU_TOKEN_REFRESH_CODES:
        return True
    lower = (msg or "").lower()
    return "token" in lower and any(
        k in lower for k in ("expired", "expire", "invalid", "过期", "失效")
    )


def format_feishu_error(code: Any, msg: str) -> str:
    """拼接官方排查建议，便于日志排查。"""
    c = _normalize_code(code)
    hint = FEISHU_ERROR_HINTS.get(c) if c is not None else None
    base = f"飞书 API 失败(code={code}): {msg}"
    return f"{base}；{hint}" if hint else base
