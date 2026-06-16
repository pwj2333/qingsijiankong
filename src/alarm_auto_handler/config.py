from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AutoHandleConfig:
    query_status: str
    is_valid: int
    handle_method: str
    handle_result: str
    is_archive: int
    remark: str


@dataclass(slots=True)
class AppConfig:
    account: str
    password: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    lookback_hours: int
    page_size: int
    order_by: int
    log_dir: Path
    excel_log_path: Path
    processed_store_path: Path
    debug: bool
    auto_handle: AutoHandleConfig


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))

    auto_handle_payload = payload["auto_handle"]
    auto_handle = AutoHandleConfig(
        query_status=str(auto_handle_payload.get("query_status", "2")),
        is_valid=int(auto_handle_payload.get("is_valid", 0)),
        handle_method=str(auto_handle_payload.get("handle_method", "识别错误")),
        handle_result=str(auto_handle_payload.get("handle_result", "")),
        is_archive=int(auto_handle_payload.get("is_archive", 0)),
        remark=str(auto_handle_payload.get("remark", "")),
    )

    return AppConfig(
        account=str(payload["account"]),
        password=str(payload["password"]),
        poll_interval_seconds=max(5, int(payload.get("poll_interval_seconds", 30))),
        request_timeout_seconds=max(5, int(payload.get("request_timeout_seconds", 20))),
        lookback_hours=max(1, int(payload.get("lookback_hours", 24))),
        page_size=max(1, int(payload.get("page_size", 50))),
        order_by=int(payload.get("order_by", 0)),
        log_dir=Path(payload.get("log_dir", "logs")),
        excel_log_path=Path(payload.get("excel_log_path", "logs/alarm_handle_log.xlsx")),
        processed_store_path=Path(payload.get("processed_store_path", "data/processed_ids.json")),
        debug=bool(payload.get("debug", False)),
        auto_handle=auto_handle,
    )
