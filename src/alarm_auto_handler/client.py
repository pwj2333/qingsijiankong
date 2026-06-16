from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests


class ApiError(RuntimeError):
    """Raised when the remote API returns an error."""


@dataclass(slots=True)
class TokenInfo:
    access_token: str
    expires_at: datetime


class QinSiApiClient:
    LOGIN_URL = "https://haiwang.kinthtech.com.cn/msaPlatform/platform/user/login"
    FIND_ALARM_URL = "https://haiwang.kinthtech.com.cn/ferryEye/captain/findExpAlarm"
    HANDLE_ALARM_URL = "https://haiwang.kinthtech.com.cn/ferryEye/captain/expAlarmHanding"

    def __init__(self, account: str, password: str, timeout_seconds: int) -> None:
        self.account = account
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.token_info: TokenInfo | None = None

    def _md5_password(self) -> str:
        return hashlib.md5(self.password.encode("utf-8")).hexdigest()

    def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        auth_required: bool = True,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if auth_required:
            self.ensure_token()
            headers["Authorization"] = self.token_info.access_token  # type: ignore[union-attr]

        response = self.session.request(
            method=method,
            url=url,
            data=data,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise ApiError(
                f"接口调用失败: code={payload.get('code')}, msg={payload.get('msg') or payload.get('message')}"
            )
        return payload

    def login(self) -> TokenInfo:
        payload = self._request(
            "POST",
            self.LOGIN_URL,
            data={"account": self.account, "password": self._md5_password()},
            auth_required=False,
        )
        item = payload.get("item") or {}
        access_token = str(item.get("accessToken", "")).strip()
        if not access_token:
            raise ApiError("登录成功但未返回 accessToken")

        expires_in = int(item.get("expiresIn", 7200))
        expires_at = datetime.now() + timedelta(seconds=max(60, expires_in - 300))
        self.token_info = TokenInfo(access_token=access_token, expires_at=expires_at)
        return self.token_info

    def ensure_token(self) -> None:
        if self.token_info is None or datetime.now() >= self.token_info.expires_at:
            self.login()

    def find_alarms(
        self,
        *,
        status: str,
        start_time: str,
        end_time: str,
        page_num: int,
        page_size: int,
        order_by: int,
    ) -> dict[str, Any]:
        try:
            return self._request(
                "POST",
                self.FIND_ALARM_URL,
                data={
                    "status": status,
                    "startTime": start_time,
                    "endTime": end_time,
                    "pageNum": page_num,
                    "pageSize": page_size,
                    "orderBy": order_by,
                },
            )
        except ApiError as exc:
            if "token" in str(exc).lower() or "401" in str(exc):
                self.token_info = None
            raise

    def handle_alarm(
        self,
        *,
        alarm_id: str,
        is_valid: int,
        handle_method: str,
        handle_result: str,
        is_archive: int,
        remark: str,
    ) -> dict[str, Any]:
        try:
            return self._request(
                "POST",
                self.HANDLE_ALARM_URL,
                data={
                    "id": alarm_id,
                    "isValid": is_valid,
                    "handleMethod": handle_method,
                    "handleResult": handle_result,
                    "isArchive": is_archive,
                    "remark": remark,
                },
            )
        except ApiError as exc:
            if "token" in str(exc).lower() or "401" in str(exc):
                self.token_info = None
            raise
