from __future__ import annotations

from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now() -> datetime:
    return datetime.now(BEIJING)


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def alarm_timestamp(alarm: dict) -> int | None:
    for key in ("happenTime", "alarmTime"):
        value = alarm.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=BEIJING)
            return int(parsed.timestamp())
        except (ValueError, OverflowError, OSError):
            continue
    return None
