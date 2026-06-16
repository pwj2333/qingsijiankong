from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import QinSiApiClient
from .config import load_config
from .excel_logger import ExcelAlarmLogger
from .logging_setup import setup_logging
from .service import AlarmAutoHandleService
from .store import ProcessedAlarmStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="勤思异常报警自动处理系统")
    parser.add_argument(
        "--config",
        default="config.json",
        help="配置文件路径，默认读取当前目录下的 config.json",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅执行一轮查询和处理，便于调试",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(config.log_dir, config.debug)
    store = ProcessedAlarmStore(config.processed_store_path)
    excel_logger = ExcelAlarmLogger(config.excel_log_path)
    client = QinSiApiClient(
        account=config.account,
        password=config.password,
        timeout_seconds=config.request_timeout_seconds,
    )
    service = AlarmAutoHandleService(
        config=config,
        client=client,
        store=store,
        excel_logger=excel_logger,
        logger=logger,
    )

    logger.info("程序启动，配置文件: %s", Path(args.config).resolve())

    try:
        if args.once:
            client.login()
            logger.info("登录成功，开始执行单轮处理")
            service.process_once()
        else:
            service.run_forever()
    except KeyboardInterrupt:
        logger.info("接收到停止信号，程序退出")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("程序异常退出: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
