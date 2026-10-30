from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from .config import EmailConfig
from .store import StateStore
from .time_utils import alarm_timestamp, format_time, now


class SmtpMailer:
    def __init__(self, config: EmailConfig, timeout_seconds: int) -> None:
        self.config, self.timeout = config, timeout_seconds

    def message(self, recipient: str, subject: str, body: str, message_id: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = recipient
        message["Subject"] = " ".join(subject.split())[:180]
        message["Date"] = format_datetime(now())
        message["Message-ID"] = message_id
        message.set_content(body)
        return message

    def send(self, message: EmailMessage, recipient: str) -> None:
        context = ssl.create_default_context()
        if self.config.tls_mode == "ssl":
            smtp = smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port,
                                    timeout=self.timeout, context=context)
        else:
            smtp = smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=self.timeout)
        try:
            smtp.ehlo()
            if self.config.tls_mode == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(self.config.username, self.config.authorization_code)
            refused = smtp.send_message(message, from_addr=self.config.sender, to_addrs=[recipient])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        finally:
            # A failed QUIT after successful DATA must not turn an accepted email into a retry.
            smtp.close()

    def send_test(self, recipient: str) -> None:
        self.send(self.message(recipient, "勤思报警系统邮件配置测试",
                               "这是一封邮件配置测试邮件。未登录或修改勤思平台报警。",
                               make_msgid(domain="qingsijiankong.local")), recipient)


class MailWorker:
    def __init__(self, store: StateStore, mailer: SmtpMailer, retries: tuple[int, ...],
                 logger: logging.Logger, excel_logger) -> None:
        self.store, self.mailer, self.retries = store, mailer, retries
        self.logger, self.excel_logger = logger, excel_logger

    def _message(self, task: dict) -> EmailMessage:
        alarm, expected = task["snapshot"], task["expected"]
        occurred = alarm_timestamp(alarm)
        subject = f"勤思报警提醒｜{alarm.get('shipName') or '未提供船名'}｜{alarm.get('behavior', '')}"
        body = (
            "勤思船舶报警提醒\n\n"
            f"船名：{alarm.get('shipName') or '未提供'}\n"
            f"报警类型：{alarm.get('behavior') or '未提供'}\n"
            f"发生时间（北京时间）：{format_time(occurred) if occurred is not None else '未提供'}\n"
            f"风险等级：{alarm.get('riskLevel') or alarm.get('alarmLevel') or '未提供'}\n"
            f"报警内容：{alarm.get('alarmContent') or '未提供'}\n"
            f"报警 ID：{task['alarm_id']}\n\n"
            f"平台结果：已标记有效，处理方式“{expected['handle_method']}”。\n"
            "系统按配置自动处理。请负责人登录平台核实现场情况，并按实际情况跟进。\n"
            "邮件发送和平台处理记录不代表现场已核实或整改已完成。\n"
        )
        return self.mailer.message(task["recipient"], subject, body, task["message_id"])

    def drain_due(self, stop: threading.Event | None = None) -> None:
        cutoff = time.time()
        while stop is None or not stop.is_set():
            tasks = self.store.due_mail(cutoff)
            if not tasks:
                return
            for task in tasks:
                if stop is not None and stop.is_set():
                    return
                try:
                    self.mailer.send(self._message(task), task["recipient"])
                except Exception as exc:
                    error = type(exc).__name__
                    delay = self.retries[min(task["attempts"], len(self.retries) - 1)] * 60
                    self.store.mail_result(task["id"], accepted=False, next_attempt=time.time() + delay, error=error)
                    result = "邮件发送失败"
                    message = f"任务 {task['id']}，{delay // 60} 分钟后重试（{error}）"
                else:
                    self.store.mail_result(task["id"], accepted=True)
                    result = "SMTP 已接受"
                    message = f"任务 {task['id']}，邮件服务器已接受，未确认用户阅读"
                self.logger.info("%s，ID=%s，%s", result, task["alarm_id"], message)
                self.excel_logger.append_row(event_type="邮件通知", result=result, message=message,
                                             alarm_id=task["alarm_id"],
                                             ship_name=str(task["snapshot"].get("shipName") or ""),
                                             behavior=str(task["snapshot"].get("behavior") or ""))

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.drain_due(stop)
            except Exception as exc:
                self.logger.error("邮件任务执行异常（%s），任务保留等待重试", type(exc).__name__)
            stop.wait(2)
