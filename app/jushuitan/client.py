from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.failure_log import log_failure
from app.jushuitan.error_codes import (
    format_jst_error,
    is_rate_limit_error,
    is_timestamp_retry_error,
    is_token_refresh_error,
)
from app.jushuitan.sign import biz_json, jst_sign, random_code
from app.jushuitan.token_store import TokenManager, TokenStore

logger = logging.getLogger(__name__)

JST_BASE = "https://openapi.jushuitan.com"
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_BASE_DELAY_SECONDS = 2.0


class JushuitanApiError(Exception):
    def __init__(self, message: str, *, code: int | None = None, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


class JushuitanClient:
    def __init__(self, app_key: str, app_secret: str, token_file: str) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._token_manager = TokenManager(TokenStore(token_file))

    def _timestamp(self) -> str:
        return str(int(time.time()))

    def _auth_post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        params["sign"] = jst_sign(params, self._app_secret)
        url = f"{JST_BASE}{path}"
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    url,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError:
            raise
        if body.get("code") != 0:
            code = body.get("code")
            msg = str(body.get("msg") or "")
            err = JushuitanApiError(format_jst_error(code, msg), code=code, raw=body)
            raise err
        data = body.get("data")
        if not isinstance(data, dict):
            raise JushuitanApiError("鉴权响应缺少 data", raw=body)
        return data

    def _fetch_init_token(self) -> dict[str, Any]:
        params = {
            "app_key": self._app_key,
            "timestamp": self._timestamp(),
            "grant_type": "authorization_code",
            "charset": "utf-8",
            "code": random_code(6),
        }
        return self._auth_post("/openWeb/auth/getInitToken", params)

    def _fetch_refresh_token(self, refresh_token: str) -> dict[str, Any]:
        params = {
            "app_key": self._app_key,
            "timestamp": self._timestamp(),
            "grant_type": "refresh_token",
            "charset": "utf-8",
            "refresh_token": refresh_token,
            "scope": "all",
        }
        return self._auth_post("/openWeb/auth/refreshToken", params)

    def _access_token(self, *, force: bool = False) -> str:
        return self._token_manager.get_valid_token(
            fetch_init=self._fetch_init_token,
            fetch_refresh=self._fetch_refresh_token,
            force=force,
        )

    def _biz_post(
        self,
        path: str,
        biz: Any,
        *,
        retry_on_token_error: bool = True,
        retry_on_timestamp_error: bool = True,
        retry_on_rate_limit: bool = True,
        rate_limit_attempt: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "app_key": self._app_key,
            "access_token": self._access_token(),
            "timestamp": self._timestamp(),
            "charset": "utf-8",
            "version": "2",
            "biz": biz_json(biz),
        }
        params["sign"] = jst_sign(params, self._app_secret)
        url = f"{JST_BASE}{path}"
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    url,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError:
            raise

        code = body.get("code")
        msg = str(body.get("msg") or "")
        if code != 0:
            if retry_on_token_error and is_token_refresh_error(code, msg):
                logger.warning(
                    "聚水潭 API token 超时/失效(code=%s msg=%s)，刷新 access_token 后重试: %s",
                    code,
                    msg,
                    path,
                )
                self._token_manager.invalidate()
                self._access_token(force=True)
                return self._biz_post(
                    path,
                    biz,
                    retry_on_token_error=False,
                    retry_on_timestamp_error=retry_on_timestamp_error,
                    retry_on_rate_limit=retry_on_rate_limit,
                    rate_limit_attempt=rate_limit_attempt,
                )
            if retry_on_timestamp_error and is_timestamp_retry_error(code):
                logger.warning(
                    "聚水潭 API 时间戳无效(code=%s)，使用新 timestamp 重试: %s",
                    code,
                    path,
                )
                return self._biz_post(
                    path,
                    biz,
                    retry_on_token_error=False,
                    retry_on_timestamp_error=False,
                    retry_on_rate_limit=retry_on_rate_limit,
                    rate_limit_attempt=rate_limit_attempt,
                )
            if (
                retry_on_rate_limit
                and is_rate_limit_error(code)
                and rate_limit_attempt < _RATE_LIMIT_MAX_RETRIES
            ):
                delay = _RATE_LIMIT_BASE_DELAY_SECONDS * (2**rate_limit_attempt)
                logger.warning(
                    "聚水潭 API 调用过频(code=%s)，%s 秒后重试(%s/%s): %s",
                    code,
                    delay,
                    rate_limit_attempt + 1,
                    _RATE_LIMIT_MAX_RETRIES,
                    path,
                )
                time.sleep(delay)
                return self._biz_post(
                    path,
                    biz,
                    retry_on_token_error=retry_on_token_error,
                    retry_on_timestamp_error=retry_on_timestamp_error,
                    retry_on_rate_limit=retry_on_rate_limit,
                    rate_limit_attempt=rate_limit_attempt + 1,
                )
            err = JushuitanApiError(format_jst_error(code, msg), code=code, raw=body)
            raise err
        data = body.get("data")
        return data if isinstance(data, dict) else {"raw": data}

    def upload_orders(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not orders:
            return []
        data = self._biz_post("/open/jushuitan/orders/upload", orders)
        datas = data.get("datas") or []
        if not isinstance(datas, list):
            return []
        return [d for d in datas if isinstance(d, dict)]

    def query_logistics(self, so_ids: list[str]) -> list[dict[str, Any]]:
        if not so_ids:
            return []
        data = self._biz_post("/open/logistic/query", {"so_ids": so_ids})
        orders = data.get("orders") or data.get("datas") or data.get("order") or []
        if isinstance(orders, dict):
            orders = [orders]
        if not isinstance(orders, list):
            return []
        return [o for o in orders if isinstance(o, dict)]

    def query_shops_all(self) -> list[dict[str, Any]]:
        shops: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._biz_post(
                "/open/shops/query",
                {"page_index": page, "page_size": 100},
            )
            batch = data.get("shops") or []
            if not isinstance(batch, list):
                batch = []
            shops.extend([s for s in batch if isinstance(s, dict)])
            if not data.get("has_next") or not batch:
                break
            page += 1
        logger.info("已加载聚水潭店铺列表 %s 条", len(shops))
        return shops

    def query_shops_map(self) -> dict[str, str]:
        """shop_id -> shop_name，用于订单缺少 shop_name 时回填。"""
        mapping: dict[str, str] = {}
        for shop in self.query_shops_all():
            shop_id = shop.get("shop_id")
            shop_name = shop.get("shop_name")
            if shop_id is None or not shop_name:
                continue
            mapping[str(shop_id).strip()] = str(shop_name).strip()
        return mapping

    def query_orders(
        self,
        *,
        modified_begin: str,
        modified_end: str,
        page_index: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], bool]:
        biz = {
            "modified_begin": modified_begin,
            "modified_end": modified_end,
            "page_index": page_index,
            "page_size": page_size,
        }
        data = self._biz_post("/open/orders/single/query", biz)
        orders = data.get("orders") or []
        if not isinstance(orders, list):
            orders = []
        has_next = bool(data.get("has_next"))
        return [o for o in orders if isinstance(o, dict)], has_next

    def query_orders_all(
        self,
        *,
        modified_begin: str,
        modified_end: str,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        all_orders: list[dict[str, Any]] = []
        page = 1
        while True:
            orders, has_next = self.query_orders(
                modified_begin=modified_begin,
                modified_end=modified_end,
                page_index=page,
                page_size=page_size,
            )
            all_orders.extend(orders)
            if not has_next or not orders:
                break
            page += 1
        return all_orders

    def query_skus(
        self,
        sku_ids: list[str],
        *,
        chunk_size: int = 50,
    ) -> dict[str, str]:
        """批量查询 SKU，返回 sku_id -> 标准品名(name)。"""
        if not sku_ids:
            return {}
        mapping: dict[str, str] = {}
        unique = list(dict.fromkeys(s.strip() for s in sku_ids if s and str(s).strip()))
        for i in range(0, len(unique), chunk_size):
            chunk = unique[i : i + chunk_size]
            biz: dict[str, Any] = {
                "page_index": 1,
                "page_size": len(chunk),
                "sku_ids": ",".join(chunk),
            }
            data = self._biz_post("/open/sku/query", biz)
            items = data.get("datas") or data.get("skus") or data.get("items") or []
            if not isinstance(items, list):
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                sid = str(item.get("sku_id") or item.get("i_id") or "").strip()
                name = str(
                    item.get("name")
                    or item.get("sku_name")
                    or item.get("standard_name")
                    or ""
                ).strip()
                if sid and name:
                    mapping[sid] = name
        return mapping
