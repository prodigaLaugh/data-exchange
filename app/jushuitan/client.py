from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.failure_log import log_failure
from app.jushuitan.error_codes import (
    format_jst_error,
    is_timestamp_retry_error,
    is_token_refresh_error,
)
from app.jushuitan.sign import biz_json, jst_sign, random_code
from app.jushuitan.token_store import TokenManager, TokenStore

logger = logging.getLogger(__name__)

JST_BASE = "https://openapi.jushuitan.com"


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
        except httpx.HTTPError as e:
            log_failure(
                "jushuitan",
                f"HTTP 请求失败: {e}",
                path=path,
                context={"stage": "auth"},
                exc=e,
            )
            raise
        if body.get("code") != 0:
            code = body.get("code")
            msg = str(body.get("msg") or "")
            err = JushuitanApiError(format_jst_error(code, msg), code=code, raw=body)
            log_failure("jushuitan", str(err), code=code, path=path, context={"stage": "auth"})
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
        except httpx.HTTPError as e:
            log_failure(
                "jushuitan",
                f"HTTP 请求失败: {e}",
                path=path,
                context={"stage": "biz"},
                exc=e,
            )
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
                )
            err = JushuitanApiError(format_jst_error(code, msg), code=code, raw=body)
            log_failure("jushuitan", str(err), code=code, path=path, context={"stage": "biz"})
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
