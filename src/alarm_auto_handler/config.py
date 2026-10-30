from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    tls_mode: str
    username: str = field(repr=False)
    authorization_code: str = field(repr=False)
    sender: str
    recipients: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Credentials:
    account: str = field(repr=False)
    password: str = field(repr=False)
    email: EmailConfig

    def secrets(self) -> tuple[str, ...]:
        import hashlib
        return (self.account, self.password, hashlib.md5(self.password.encode()).hexdigest(),
                self.email.username, self.email.authorization_code)


@dataclass(frozen=True, slots=True)
class AutoHandleConfig:
    valid_handle_method: str = "通知已处理"
    invalid_handle_method: str = "识别错误"
    handle_result: str = "系统按配置自动处理，请负责人核实现场情况"
    remark: str = "系统按配置自动处理"
    is_archive: int = 0


@dataclass(frozen=True, slots=True)
class AppConfig:
    log_dir: Path
    excel_log_path: Path
    state_db_path: Path
    processed_store_path: Path
    poll_interval_seconds: int = 30
    request_timeout_seconds: int = 20
    email_timeout_seconds: int = 20
    lookback_hours: int = 24
    page_size: int = 50
    order_by: int = 0
    debug: bool = False
    keywords: tuple[str, ...] = ("人员离岗", "未定期巡检", "未定期巡查")
    probability: float = 0.5
    email_retry_minutes: tuple[int, ...] = (1, 5, 15, 30)
    auto_handle: AutoHandleConfig = field(default_factory=AutoHandleConfig)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ConfigError(f"无法读取配置文件或 JSON 格式错误：{path.name}") from None
    if not isinstance(value, dict):
        raise ConfigError(f"配置文件必须是 JSON 对象：{path.name}")
    return value


def _text(data: dict, key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ConfigError(f"{key} 必须填写非空单行文本")
    if value.startswith(("填写", "your_")):
        raise ConfigError(f"请填写 {key}，不能使用模板占位值")
    return value.strip()


def _integer(data: dict, key: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    value = data.get(key, default)
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{key} 必须是范围内的整数（最小 {minimum}）")
    return value


def _section(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key} 必须是 JSON 对象")
    return value


def _address(value: object) -> str:
    if not isinstance(value, str) or any(c.isspace() for c in value) or value.count("@") != 1:
        raise ConfigError("邮箱必须填写完整地址，不包含显示名称或空白字符")
    local, domain = value.split("@")
    if not local or "." not in domain or any(c in value for c in "<>,;\x00"):
        raise ConfigError("邮箱地址格式不正确")
    if domain.lower() == "example.com":
        raise ConfigError("请将示例邮箱替换为真实邮箱")
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ConfigError("邮箱地址需使用 ASCII 字符") from None
    return value


def load_credentials(path: str | Path) -> Credentials:
    payload = _read(Path(path).expanduser().resolve())
    platform = _section(payload, "platform")
    email = _section(payload, "email")
    mode = _text(email, "tls_mode").lower()
    if mode not in ("ssl", "starttls"):
        raise ConfigError("tls_mode 仅支持 ssl 或 starttls")
    recipients = email.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise ConfigError("recipients 至少需要填写一个收件邮箱")
    _text(platform, "password")
    _text(email, "authorization_code")
    return Credentials(
        account=_text(platform, "account"), password=platform["password"],
        email=EmailConfig(
            smtp_host=_text(email, "smtp_host"),
            smtp_port=_integer(email, "smtp_port", 465 if mode == "ssl" else 587, maximum=65535),
            tls_mode=mode, username=_text(email, "username"),
            authorization_code=email["authorization_code"],
            sender=_address(email.get("sender")),
            recipients=tuple(dict.fromkeys(_address(v) for v in recipients)),
        ),
    )


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    payload = _read(path)
    if "account" in payload or "password" in payload:
        raise ConfigError("请将 account 和 password 移入独立的 credentials.json")
    sampling = _section(payload, "sampling")
    keywords = sampling.get("keywords", ["人员离岗", "未定期巡检", "未定期巡查"])
    if not isinstance(keywords, list) or not keywords or any(not isinstance(v, str) or not v.strip() for v in keywords):
        raise ConfigError("sampling.keywords 必须是非空关键词数组")
    probability = sampling.get("probability", 0.5)
    if type(probability) not in (float, int) or not 0 <= probability <= 1:
        raise ConfigError("sampling.probability 必须在 0 到 1 之间")
    retries = payload.get("email_retry_minutes", [1, 5, 15, 30])
    if not isinstance(retries, list) or not retries or any(type(v) is not int or v < 1 for v in retries):
        raise ConfigError("email_retry_minutes 必须是正整数数组")
    debug = payload.get("debug", False)
    if type(debug) is not bool:
        raise ConfigError("debug 必须是布尔值")
    auto = _section(payload, "auto_handle")

    def output_path(key: str, default: str) -> Path:
        result = Path(_text(payload, key, default)).expanduser()
        return result if result.is_absolute() else path.parent / result

    return AppConfig(
        log_dir=output_path("log_dir", "logs"),
        excel_log_path=output_path("excel_log_path", "logs/alarm_handle_log.xlsx"),
        state_db_path=output_path("state_db_path", "data/alarm_state.sqlite3"),
        processed_store_path=output_path("processed_store_path", "data/processed_ids.json"),
        poll_interval_seconds=_integer(payload, "poll_interval_seconds", 30, 5),
        request_timeout_seconds=_integer(payload, "request_timeout_seconds", 20, 1),
        email_timeout_seconds=_integer(payload, "email_timeout_seconds", 20, 1),
        lookback_hours=_integer(payload, "lookback_hours", 24),
        page_size=_integer(payload, "page_size", 50),
        order_by=_integer(payload, "order_by", 0, 0), debug=debug,
        keywords=tuple(v.strip() for v in keywords), probability=float(probability),
        email_retry_minutes=tuple(retries),
        auto_handle=AutoHandleConfig(
            valid_handle_method=_text(auto, "valid_handle_method", "通知已处理"),
            invalid_handle_method=_text(auto, "invalid_handle_method", "识别错误"),
            handle_result=_text(auto, "handle_result", "系统按配置自动处理，请负责人核实现场情况"),
            remark=_text(auto, "remark", "系统按配置自动处理"),
            is_archive=_integer(auto, "is_archive", 0, 0, 1),
        ),
    )
