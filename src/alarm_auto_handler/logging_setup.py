from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .time_utils import BEIJING


class BeijingFormatter(logging.Formatter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__("%(asctime)s [%(levelname)s] %(message)s")
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, BEIJING).strftime("%Y-%m-%d %H:%M:%S")

    def format(self, record):
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text


def setup_logging(log_dir: Path, debug: bool, secrets: tuple[str, ...] = ()) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("alarm_auto_handler")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
    formatter = BeijingFormatter(secrets)
    file_handler = RotatingFileHandler(log_dir / "alarm_auto_handler.log", maxBytes=2 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
    for handler in (file_handler, logging.StreamHandler()):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.propagate = False
    return logger
