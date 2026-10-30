from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alarm_auto_handler.client import ApiError, QinSiApiClient, TokenInfo
from alarm_auto_handler.config import ConfigError, EmailConfig, load_config, load_credentials
from alarm_auto_handler.logging_setup import BeijingFormatter
from alarm_auto_handler.mailer import SmtpMailer
from alarm_auto_handler.main import main, single_instance
from alarm_auto_handler.store import StateStore
from alarm_auto_handler.time_utils import alarm_timestamp, format_time


def response(status=200, payload=None):
    result = Mock(status_code=status)
    result.json.return_value = payload if payload is not None else {"success": True, "item": {"list": [], "totalPage": 0}}
    return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = QinSiApiClient("platform-user", "very-secret-password", 5)
        self.addCleanup(self.client.session.close)
        self.client.token_info = TokenInfo("old-token", time.monotonic() + 1000)

    def test_http401_refreshes_token_and_retries_once(self):
        self.client.session.request = Mock(side_effect=[response(401), response(payload={
            "success": True, "item": {"accessToken": "new-token", "expiresIn": 7200}}), response()])
        self.client.find_alarms(status="2", start_time="", end_time="", page_num=1, page_size=50, order_by=0)
        calls = self.client.session.request.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].kwargs["headers"]["Authorization"], "old-token")
        self.assertEqual(calls[2].kwargs["headers"]["Authorization"], "new-token")

    def test_repeated_http401_is_bounded_and_clears_token(self):
        self.client.session.request = Mock(side_effect=[response(401), response(payload={
            "success": True, "item": {"accessToken": "new-token"}}), response(401)])
        with self.assertRaisesRegex(ApiError, "401"):
            self.client.handle_alarm(alarm_id="1", is_valid=1, handle_method="通知已处理", handle_result="", is_archive=0, remark="")
        self.assertEqual(self.client.session.request.call_count, 3)
        self.assertIsNone(self.client.token_info)

    def test_timeout_and_invalid_json_are_uncertain_and_errors_do_not_echo_secrets(self):
        for result in (requests.Timeout("very-secret-password"), response(503)):
            self.client.session.request = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
            with self.assertRaises(ApiError) as context:
                self.client._request("POST", "https://unit.test/handle")
            self.assertTrue(context.exception.outcome_unknown)
            self.assertNotIn("very-secret-password", str(context.exception))
        self.client.session.request = Mock(return_value=response(payload={"success": False, "msg": "very-secret-password"}))
        with self.assertRaises(ApiError) as context:
            self.client._request("POST", "https://unit.test/handle")
        self.assertNotIn("very-secret-password", str(context.exception))


class ConfigAndRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config_path = self.root / "config.json"
        self.config_path.write_text("{}")
        self.credentials_path = self.root / "credentials.json"
        self.payload = {"platform": {"account": "platform-user", "password": "secret-password"},
                        "email": {"smtp_host": "smtp.unit.test", "tls_mode": "ssl", "username": "smtp-user",
                                  "authorization_code": "secret-smtp-code", "sender": "sender@unit.test",
                                  "recipients": ["one@unit.test", "two@unit.test"]}}
        self.write_credentials()
        self.addCleanup(self.close_logs)

    def close_logs(self):
        logger = logging.getLogger("alarm_auto_handler")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def write_credentials(self):
        self.credentials_path.write_text(json.dumps(self.payload), encoding="utf-8")

    def test_paths_are_relative_to_config_and_credentials_are_separate(self):
        config = load_config(self.config_path)
        self.assertEqual(config.state_db_path, self.root.resolve() / "data/alarm_state.sqlite3")
        credentials = load_credentials(self.credentials_path)
        self.assertEqual(credentials.email.recipients, ("one@unit.test", "two@unit.test"))
        self.assertNotIn("secret-password", repr(credentials))
        self.assertNotIn("secret-smtp-code", repr(credentials))

    def test_invalid_configuration_is_rejected_without_values_in_errors(self):
        for payload in ({"sampling": {"probability": 2}}, {"page_size": 0}, {"debug": "false"},
                        {"email_retry_minutes": []}, {"account": "secret-account"}):
            self.config_path.write_text(json.dumps(payload))
            with self.assertRaises(ConfigError) as context:
                load_config(self.config_path)
            self.assertNotIn("secret-account", str(context.exception))
        self.payload["email"]["tls_mode"] = "none"
        self.write_credentials()
        with self.assertRaises(ConfigError):
            load_credentials(self.credentials_path)

    def test_mail_test_command_does_not_create_state_or_touch_platform(self):
        with patch.object(sys, "argv", ["run.py", "--config", str(self.config_path), "--test-email"]), \
                patch("alarm_auto_handler.main.QinSiApiClient") as client, \
                patch.object(SmtpMailer, "send_test") as send:
            self.assertEqual(main(), 0)
            self.assertEqual(send.call_count, 2)
            client.assert_not_called()
        self.assertFalse((self.root / "data").exists())

    def test_bad_credentials_exit_cleanly_without_state(self):
        self.payload["platform"]["password"] = ""
        self.write_credentials()
        with patch.object(sys, "argv", ["run.py", "--config", str(self.config_path)]), patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(main(), 2)
        self.assertFalse((self.root / "data").exists())

    def test_once_integrates_real_http_forms_sqlite_excel_and_mail_queue(self):
        occurred = int(time.time()) - 10
        store = StateStore(self.root / "data/alarm_state.sqlite3", started_at=occurred - 60)
        alarms = [
            {"id": "notify", "behavior": "未定期巡查机舱", "happenTime": format_time(occurred), "status": 2},
            {"id": "invalid", "behavior": "其他类型", "happenTime": format_time(occurred), "status": 2},
        ]
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode(), keep_blank_values=True)
                calls.append((self.path, body, self.headers.get("Authorization")))
                if self.path == "/login":
                    item = {"accessToken": "local-only-token", "expiresIn": 7200}
                elif self.path == "/find":
                    pending = [a for a in alarms if a["status"] == 2]
                    page = int(body["pageNum"][0])
                    item = {"list": pending[page - 1:page], "totalPage": len(pending)}
                else:
                    target = next(a for a in alarms if a["id"] == body["id"][0])
                    target["status"] = 0
                    item = {}
                raw = json.dumps({"success": True, "item": item}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        self.config_path.write_text(json.dumps({"page_size": 1}))
        try:
            with patch.object(sys, "argv", ["run.py", "--config", str(self.config_path), "--once"]), \
                    patch.object(QinSiApiClient, "LOGIN_URL", base + "/login"), \
                    patch.object(QinSiApiClient, "FIND_ALARM_URL", base + "/find"), \
                    patch.object(QinSiApiClient, "HANDLE_ALARM_URL", base + "/handle"), \
                    patch("alarm_auto_handler.service.random.SystemRandom.random", return_value=0.1), \
                    patch.object(SmtpMailer, "send") as send:
                self.assertEqual(main(), 0)
                self.assertEqual(send.call_count, 2)
            self.assertEqual([c[0] for c in calls], ["/login", "/find", "/find", "/handle", "/handle"])
            handles = {body["id"][0]: body for path, body, _ in calls if path == "/handle"}
            self.assertEqual(handles["notify"]["isValid"], ["1"])
            self.assertEqual(handles["notify"]["handleMethod"], ["通知已处理"])
            self.assertEqual(handles["invalid"]["isValid"], ["0"])
            self.assertEqual(store.counts(), {"accepted": 2})
            self.assertTrue((self.root / "logs/alarm_handle_log.xlsx").is_file())
            text = (self.root / "logs/alarm_auto_handler.log").read_text(encoding="utf-8")
            for secret in ("secret-password", "secret-smtp-code", "local-only-token"):
                self.assertNotIn(secret, text)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_lock_prevents_two_instances_on_same_database(self):
        path = self.root / "state.lock"
        with single_instance(path):
            with self.assertRaises(RuntimeError):
                with single_instance(path):
                    self.fail("second instance acquired lock")

    def test_beijing_time_is_independent_of_host_and_secrets_are_redacted(self):
        formatter = BeijingFormatter(("secret-password", "secret-code"))
        record = logging.LogRecord("test", logging.INFO, "", 0, "secret-password secret-code", (), None)
        record.created = 0
        self.assertEqual(formatter.format(record), "1970-01-01 08:00:00 [INFO] [REDACTED] [REDACTED]")
        self.assertEqual(format_time(0), "1970-01-01 08:00:00")
        self.assertEqual(alarm_timestamp({"happenTime": "bad", "alarmTime": "1970-01-01 08:00:00"}), 0)
        self.assertEqual(alarm_timestamp({"alarmTime": "1970-01-01T00:00:00Z"}), 0)


class SmtpTests(unittest.TestCase):
    def test_ssl_and_starttls_use_verified_tls_and_individual_envelopes(self):
        for mode, port in (("ssl", 465), ("starttls", 587)):
            with self.subTest(mode=mode), patch("alarm_auto_handler.mailer.smtplib.SMTP_SSL") as ssl_smtp, \
                    patch("alarm_auto_handler.mailer.smtplib.SMTP") as plain_smtp:
                config = EmailConfig("smtp.unit.test", port, mode, "smtp-user", "secret-auth", "sender@unit.test", ("one@unit.test",))
                mailer = SmtpMailer(config, 5)
                constructor = ssl_smtp if mode == "ssl" else plain_smtp
                smtp = constructor.return_value
                smtp.send_message.return_value = {}
                mailer.send_test("one@unit.test")
                smtp.login.assert_called_once_with("smtp-user", "secret-auth")
                if mode == "starttls":
                    smtp.starttls.assert_called_once()
                    self.assertTrue(smtp.starttls.call_args.kwargs["context"].check_hostname)
                else:
                    self.assertTrue(constructor.call_args.kwargs["context"].check_hostname)

    def test_smtp_acceptance_does_not_depend_on_quit(self):
        with patch("alarm_auto_handler.mailer.smtplib.SMTP_SSL") as constructor:
            smtp = constructor.return_value
            smtp.send_message.return_value = {}
            config = EmailConfig("smtp.unit.test", 465, "ssl", "smtp-user", "secret-auth", "sender@unit.test", ("one@unit.test",))
            mailer = SmtpMailer(config, 5)
            mailer.send_test("one@unit.test")
            smtp.quit.assert_not_called()
            smtp.close.assert_called_once()
            self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], ["one@unit.test"])


if __name__ == "__main__":
    unittest.main()
