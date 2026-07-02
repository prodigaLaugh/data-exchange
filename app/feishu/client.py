from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from app.failure_log import log_failure
from app.feishu.error_codes import format_feishu_error, is_token_refresh_error

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"
# 自建应用 tenant_access_token: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
TOKEN_URL = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"

# 过期前提前刷新（秒），tenant_access_token 有效期通常为 7200s
FEISHU_REFRESH_BEFORE_SECONDS = 600


class FeishuApiError(Exception):
    def __init__(self, message: str, *, code: int | None = None, raw: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.raw = raw


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, app_token: str) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._app_token = app_token
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        self._token_lock = threading.Lock()

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expire_at = 0.0

    def _fetch_token_unlocked(self, client: httpx.Client) -> str:
        resp = client.post(
            TOKEN_URL,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            code = data.get("code")
            msg = str(data.get("msg") or "")
            err = FeishuApiError(format_feishu_error(code, msg), code=code, raw=data)
            log_failure(
                "feishu",
                str(err),
                code=code,
                path=TOKEN_URL,
                context={"stage": "tenant_access_token"},
            )
            raise err
        token = data.get("tenant_access_token")
        if not token:
            raise FeishuApiError("响应中缺少 tenant_access_token", raw=data)
        expire = int(data.get("expire", 7200))
        now = time.time()
        self._token = token
        self._token_expire_at = now + expire
        logger.info("已获取飞书 tenant_access_token，有效期约 %s 秒", expire)
        return token

    def _ensure_token(self, client: httpx.Client, *, force: bool = False) -> str:
        now = time.time()
        if (
            not force
            and self._token
            and now < self._token_expire_at - FEISHU_REFRESH_BEFORE_SECONDS
        ):
            return self._token

        with self._token_lock:
            now = time.time()
            if (
                not force
                and self._token
                and now < self._token_expire_at - FEISHU_REFRESH_BEFORE_SECONDS
            ):
                return self._token
            if force:
                logger.info("飞书 token 已失效或即将失效，重新获取 tenant_access_token")
            elif self._token:
                logger.info("飞书 token 临近过期，提前刷新 tenant_access_token")
            return self._fetch_token_unlocked(client)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retry_on_token_error: bool = True,
    ) -> dict[str, Any]:
        url = f"{FEISHU_BASE}{path}"
        try:
            with httpx.Client(timeout=60.0) as client:
                token = self._ensure_token(client)
                resp = client.request(
                    method,
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    json=json_body,
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as e:
            log_failure(
                "feishu",
                f"HTTP 请求失败: {e}",
                path=path,
                context={"method": method, "params": params},
                exc=e,
            )
            raise

        code = body.get("code")
        msg = str(body.get("msg") or "")
        if code != 0:
            if retry_on_token_error and is_token_refresh_error(code, msg):
                logger.warning(
                    "飞书 API token 失效(code=%s msg=%s)，重新获取 tenant_access_token 后重试: %s",
                    code,
                    msg,
                    path,
                )
                with httpx.Client(timeout=60.0) as client:
                    self._invalidate_token()
                    self._ensure_token(client, force=True)
                return self._request(
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    retry_on_token_error=False,
                )
            err = FeishuApiError(format_feishu_error(code, msg), code=code, raw=body)
            log_failure(
                "feishu",
                str(err),
                code=code,
                path=path,
                context={"method": method, "params": params},
            )
            raise err
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def list_all_records(self, table_id: str, *, page_size: int = 500) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self._request(
                "GET",
                f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/records",
                params=params,
            )
            items = data.get("items") or []
            if isinstance(items, list):
                records.extend([r for r in items if isinstance(r, dict)])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return records

    def batch_update_records(
        self,
        table_id: str,
        updates: list[dict[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        # https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_update
        if not updates:
            return 0
        updated = 0
        for i in range(0, len(updates), chunk_size):
            chunk = updates[i : i + chunk_size]
            self._request(
                "POST",
                f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/records/batch_update",
                json_body={"records": chunk},
            )
            updated += len(chunk)
        return updated

    def batch_create_records(
        self,
        table_id: str,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        # https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/batch_create
        if not rows:
            return 0
        created = 0
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            self._request(
                "POST",
                f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/records/batch_create",
                json_body={"records": [{"fields": f} for f in chunk]},
            )
            created += len(chunk)
        return created

    def get_record(self, table_id: str, record_id: str) -> dict[str, Any]:
        data = self._request(
            "GET",
            f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/records/{record_id}",
        )
        record = data.get("record")
        if not isinstance(record, dict):
            raise FeishuApiError("获取记录响应缺少 record", raw=data)
        return record

    def batch_get_records(
        self,
        table_id: str,
        record_ids: list[str],
        *,
        chunk_size: int = 100,
    ) -> list[dict[str, Any]]:
        if not record_ids:
            return []
        records: list[dict[str, Any]] = []
        for i in range(0, len(record_ids), chunk_size):
            chunk = record_ids[i : i + chunk_size]
            data = self._request(
                "POST",
                f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/records/batch_get",
                json_body={"record_ids": chunk},
            )
            items = data.get("records") or data.get("items") or []
            if isinstance(items, list):
                records.extend([r for r in items if isinstance(r, dict)])
        return records

    def list_table_fields(self, table_id: str) -> list[dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._request(
                "GET",
                f"/bitable/v1/apps/{self._app_token}/tables/{table_id}/fields",
                params=params,
            )
            items = data.get("items") or []
            if isinstance(items, list):
                fields.extend([f for f in items if isinstance(f, dict)])
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return fields

    def resolve_linked_table_id(
        self,
        parent_table_id: str,
        link_field_name: str,
        *,
        fallback_table_id: str = "",
    ) -> str:
        if fallback_table_id:
            return fallback_table_id
        for field in self.list_table_fields(parent_table_id):
            if field.get("field_name") != link_field_name:
                continue
            prop = field.get("property") or {}
            table_id = prop.get("table_id") or prop.get("tableId")
            if table_id:
                return str(table_id)
        return ""
