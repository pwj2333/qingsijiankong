from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

import requests


class ApiError(RuntimeError):
    """A sanitized API failure; unknown outcomes must be reconciled before retry."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


@dataclass(slots=True)
class TokenInfo:
    access_token: str
    expires_at: float


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

    def _request(self, method: str, url: str, *, data: dict[str, Any] | None = None,
                 auth_required: bool = True) -> dict[str, Any]:
        for attempt in range(2):
            headers = {}
            if auth_required:
                self.ensure_token()
                headers["Authorization"] = self.token_info.access_token
            try:
                response = self.session.request(method, url, data=data, headers=headers,
                                                timeout=self.timeout_seconds)
            except requests.RequestException:
                raise ApiError("接口网络异常，结果待核实", outcome_unknown=True) from None
            if response.status_code == 401 and auth_required:
                self.token_info = None
                if attempt == 0:
                    continue
            if response.status_code >= 400:
                raise ApiError(f"接口 HTTP {response.status_code}",
                               outcome_unknown=response.status_code >= 500 or response.status_code == 408)
            try:
                payload = response.json()
            except ValueError:
                raise ApiError("接口未返回有效 JSON", outcome_unknown=True) from None
            if not isinstance(payload, dict):
                raise ApiError("接口返回结构异常", outcome_unknown=True)
            if payload.get("success") is not True:
                # Remote error bodies can echo credentials or tokens; never log them.
                message = str(payload.get("msg") or payload.get("message") or "").lower()
                auth_failed = str(payload.get("code")) == "401" or "token" in message
                if auth_required and auth_failed:
                    self.token_info = None
                    if attempt == 0:
                        continue
                raise ApiError("接口拒绝请求，请检查平台权限、参数或账号状态")
            return payload
        raise ApiError("接口鉴权失败")

    def login(self) -> TokenInfo:
        payload = self._request("POST", self.LOGIN_URL, data={
            "account": self.account,
            "password": hashlib.md5(self.password.encode("utf-8")).hexdigest(),
        }, auth_required=False)
        item = payload.get("item") or {}
        token = item.get("accessToken") if isinstance(item, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ApiError("登录未返回 accessToken")
        try:
            lifetime = int(item.get("expiresIn", 7200))
        except (TypeError, ValueError):
            raise ApiError("登录返回的有效期格式错误") from None
        self.token_info = TokenInfo(token.strip(), time.monotonic() + max(1, lifetime - min(300, lifetime // 2)))
        return self.token_info

    def ensure_token(self) -> None:
        if self.token_info is None or time.monotonic() >= self.token_info.expires_at:
            self.login()

    def find_alarms(self, *, status: str, start_time: str, end_time: str, page_num: int,
                    page_size: int, order_by: int) -> dict[str, Any]:
        return self._request("POST", self.FIND_ALARM_URL, data={
            "status": status, "startTime": start_time, "endTime": end_time,
            "pageNum": page_num, "pageSize": page_size, "orderBy": order_by,
        })

    def handle_alarm(self, *, alarm_id: str, is_valid: int, handle_method: str,
                     handle_result: str, is_archive: int, remark: str) -> dict[str, Any]:
        return self._request("POST", self.HANDLE_ALARM_URL, data={
            "id": alarm_id, "isValid": is_valid, "handleMethod": handle_method,
            "handleResult": handle_result, "isArchive": is_archive, "remark": remark,
        })
