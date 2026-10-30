from __future__ import annotations

import argparse
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

from .client import QinSiApiClient
from .config import ConfigError, load_config, load_credentials
from .excel_logger import ExcelAlarmLogger
from .logging_setup import setup_logging
from .mailer import MailWorker, SmtpMailer
from .service import AlarmAutoHandleService
from .store import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="勤思报警分类处理与邮件通知系统")
    parser.add_argument("--config", default="config.json", help="业务配置文件路径")
    parser.add_argument("--credentials", help="凭据文件路径，默认在业务配置同目录读取 credentials.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="执行一轮查询、处理和到期邮件任务")
    mode.add_argument("--test-email", action="store_true", help="仅向配置收件人发送测试邮件，不访问平台")
    return parser


@contextmanager
def single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise RuntimeError("已有实例正在使用该状态库") from None
        yield
    finally:
        if locked:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_config(args.config)
        credentials_path = Path(args.credentials) if args.credentials else Path(args.config).expanduser().resolve().parent / "credentials.json"
        credentials = load_credentials(credentials_path)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    logger = setup_logging(config.log_dir, config.debug, credentials.secrets())
    mailer = SmtpMailer(credentials.email, config.email_timeout_seconds)
    if args.test_email:
        failures = 0
        for index, recipient in enumerate(credentials.email.recipients, 1):
            try:
                mailer.send_test(recipient)
                logger.info("测试邮件收件人 %s：SMTP 已接受，未确认用户阅读", index)
            except Exception as exc:
                failures += 1
                logger.error("测试邮件收件人 %s：发送失败（%s）", index, type(exc).__name__)
        return int(failures > 0)
    stop = threading.Event()
    worker_thread = None
    client = None
    old_handlers = {}
    try:
        with single_instance(config.state_db_path.with_suffix(".lock")):
            store = StateStore(config.state_db_path, config.processed_store_path, account=credentials.account)
            excel_logger = ExcelAlarmLogger(config.excel_log_path)
            client = QinSiApiClient(credentials.account, credentials.password, config.request_timeout_seconds)
            service = AlarmAutoHandleService(config, client, store, excel_logger, logger, credentials.email.recipients)
            worker = MailWorker(store, mailer, config.email_retry_minutes, logger, excel_logger)
            if args.once:
                try:
                    service.process_once()
                finally:
                    worker.drain_due()
                if store.actionable() or store.counts().get("pending", 0):
                    logger.warning("仍有平台或邮件任务等待重试")
                    return 1
            else:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
                worker_thread = threading.Thread(target=worker.run, args=(stop,), name="mail-worker")
                worker_thread.start()
                try:
                    service.run_forever(stop)
                finally:
                    stop.set()
                    worker_thread.join()
        return 0
    except KeyboardInterrupt:
        logger.info("接收到停止信号")
        return 0
    except (ValueError, RuntimeError) as exc:
        logger.error("程序退出：%s", exc)
        return 1
    except Exception as exc:
        logger.error("程序退出（%s），请检查配置、状态文件及运行环境", type(exc).__name__)
        return 1
    finally:
        stop.set()
        if client is not None:
            client.session.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
