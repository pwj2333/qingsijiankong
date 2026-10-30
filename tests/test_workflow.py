from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alarm_auto_handler.client import ApiError
from alarm_auto_handler.config import AppConfig, EmailConfig
from alarm_auto_handler.mailer import MailWorker, SmtpMailer
from alarm_auto_handler.service import AlarmAutoHandleService
from alarm_auto_handler.store import StateStore
from alarm_auto_handler.time_utils import alarm_timestamp, format_time

NOW = 1793329200


def alarm(alarm_id, behavior="人员离岗", occurred=NOW - 10):
    return {"id": alarm_id, "behavior": behavior, "happenTime": format_time(occurred),
            "shipName": "测试船", "riskLevel": "高", "alarmContent": "请核实现场", "status": 2}


class FakePlatform:
    def __init__(self, alarms):
        self.alarms = alarms
        self.handles = []
        self.queries = []
        self.mode = "ok"
        self.fail_page = None

    def find_alarms(self, **query):
        self.queries.append(query)
        if query["page_num"] == self.fail_page:
            raise ApiError("模拟查询失败")
        # Missing fields are deliberately returned to exercise defensive classification.
        values = [a for a in self.alarms if not query["status"] or a.get("status") == 2]
        start = alarm_timestamp({"alarmTime": query["start_time"]})
        end = alarm_timestamp({"alarmTime": query["end_time"]})
        values = [a for a in values if alarm_timestamp(a) is None or start <= alarm_timestamp(a) <= end]
        size, page = query["page_size"], query["page_num"]
        return {"success": True, "item": {"list": [dict(a) for a in values[(page - 1) * size:page * size]],
                                          "totalPage": (len(values) + size - 1) // size}}

    def handle_alarm(self, **parameters):
        self.handles.append(parameters)
        if self.mode == "reject":
            raise ApiError("模拟拒绝")
        if self.mode == "timeout_before":
            raise ApiError("模拟超时", outcome_unknown=True)
        item = next(a for a in self.alarms if a["id"] == parameters["alarm_id"])
        item.update(status=0, isValid=parameters["is_valid"], handleMethod=parameters["handle_method"],
                    handleResult=parameters["handle_result"], remark=parameters["remark"])
        if self.mode == "timeout_after":
            raise ApiError("模拟超时", outcome_unknown=True)
        return {"success": True}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = patch("time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.temp.cleanup)
        self.config = AppConfig(self.root / "logs", self.root / "logs/a.xlsx", self.root / "data/state.sqlite3",
                                self.root / "data/processed_ids.json", page_size=2)
        self.store = StateStore(self.config.state_db_path, started_at=NOW - 60)
        self.excel = Mock()
        self.logger = logging.getLogger("test-workflow")
        self.recipients = ("one@unit.test", "two@unit.test")

    def service(self, platform, config=None, draws=None, store=None):
        return AlarmAutoHandleService(config or self.config, platform, store or self.store, self.excel,
                                      self.logger, self.recipients, random_value=draws or Mock(return_value=0.1))

    def mailer(self):
        return SmtpMailer(EmailConfig("smtp.unit.test", 465, "ssl", "test-user", "test-auth",
                                     "sender@unit.test", self.recipients), 5)

    def test_classification_and_fixed_decisions_after_restart_and_rule_change(self):
        platform = FakePlatform([alarm("1"), alarm("2", "未定期巡检驾驶台"),
                                 alarm("3", "未定期巡查机舱"), alarm("4", "其他报警")])
        draws = Mock(side_effect=[0.2, 0.8, 0.49])
        self.service(platform, draws=draws).process_once()
        self.assertEqual(draws.call_count, 3)
        self.assertEqual({x["alarm_id"]: x["is_valid"] for x in platform.handles}, {"1": 1, "3": 1, "4": 0})
        self.assertEqual(self.store.get("2")["decision"], "leave")
        self.assertEqual(self.store.get("2")["platform_state"], "ignored")
        self.assertEqual(self.store.counts(), {"pending": 4})
        no_new_draw = Mock(side_effect=AssertionError("must not draw again"))
        restarted = StateStore(self.config.state_db_path, started_at=NOW + 100)
        self.assertEqual(restarted.deployment_time, NOW - 60)
        self.service(platform, replace(self.config, probability=1, keywords=("不同规则",)), no_new_draw, restarted).process_once()
        self.assertEqual(len(platform.handles), 3)
        self.assertEqual(self.store.get("2")["decision"], "leave")

    def test_history_and_missing_fields_are_not_submitted(self):
        platform = FakePlatform([alarm("old", occurred=NOW - 600), alarm("bad-type", behavior=None),
                                 dict(alarm("bad-time"), happenTime="错误时间"), alarm(None), alarm("new", "其他")])
        self.service(platform).process_once()
        self.assertEqual([a["alarm_id"] for a in platform.handles], ["new"])
        self.assertIsNone(self.store.get("old"))  # Excluded by the first deployment query boundary.
        self.assertEqual(self.store.get("bad-type")["decision"], "malformed")
        self.assertEqual(self.store.get("bad-time")["decision"], "malformed")
        self.assertIsNone(self.store.get("None"))

    def test_fetches_all_pages_before_modifying_pending_set(self):
        platform = FakePlatform([alarm(str(i), "其他") for i in range(7)])
        self.service(platform).process_once()
        self.assertEqual(len(platform.handles), 7)
        self.assertEqual(len(platform.queries), 4)

    def test_query_failure_does_not_advance_cursor_or_process_partial_results(self):
        platform = FakePlatform([alarm(str(i), "其他") for i in range(4)])
        platform.fail_page = 2
        with self.assertRaises(ApiError):
            self.service(platform).process_once()
        self.assertEqual(platform.handles, [])
        self.assertIsNone(self.store.metadata("query_cursor"))
        platform.fail_page = None
        self.service(platform).process_once()
        self.assertEqual(len(platform.handles), 4)
        self.assertEqual(int(self.store.metadata("query_cursor")), NOW)

    def test_offline_backlog_is_not_limited_to_lookback_window(self):
        with self.store.connection() as db:
            db.execute("UPDATE metadata SET value=? WHERE key='deployment_time'", (str(NOW - 20000),))
        self.store.advance_cursor(NOW - 10800)
        platform = FakePlatform([alarm("offline", "其他", NOW - 10000), alarm("recent", "其他")])
        self.service(platform, replace(self.config, lookback_hours=1)).process_once()
        self.assertEqual({a["alarm_id"] for a in platform.handles}, {"offline", "recent"})
        self.assertGreaterEqual(len(platform.queries), 4)
        self.assertEqual(platform.queries[0]["start_time"], format_time(NOW - 10860))

    def test_rejected_platform_submission_retries_without_early_mail(self):
        platform = FakePlatform([alarm("1")])
        platform.mode = "reject"
        self.service(platform).process_once()
        self.assertEqual(self.store.get("1")["platform_state"], "pending")
        self.assertEqual(self.store.counts(), {})
        platform.mode = "ok"
        self.service(platform).process_once()
        self.assertEqual(self.store.get("1")["platform_state"], "handled")
        self.assertEqual(self.store.counts(), {"pending": 2})

    def test_timeout_after_commit_reconciles_and_creates_mail_without_resubmission(self):
        platform = FakePlatform([alarm("1")])
        platform.mode = "timeout_after"
        self.service(platform).process_once()
        self.assertEqual(self.store.get("1")["platform_state"], "uncertain")
        self.assertEqual(self.store.counts(), {})
        platform.mode = "ok"
        self.service(platform, store=StateStore(self.config.state_db_path)).process_once()
        self.assertEqual(len(platform.handles), 1)
        self.assertEqual(self.store.get("1")["platform_state"], "handled")
        self.assertEqual(self.store.counts(), {"pending": 2})

    def test_crash_after_api_success_recovers_outbox(self):
        platform = FakePlatform([alarm("1")])
        with patch.object(self.store, "complete", side_effect=OSError("simulated crash")):
            self.service(platform).process_once()
        self.assertEqual(self.store.get("1")["platform_state"], "uncertain")
        self.service(platform).process_once()
        self.assertEqual(len(platform.handles), 1)
        self.assertEqual(self.store.counts(), {"pending": 2})

    def test_timeout_before_commit_retries_only_after_confirming_pending(self):
        platform = FakePlatform([alarm("1")])
        platform.mode = "timeout_before"
        self.service(platform).process_once()
        platform.mode = "ok"
        self.service(platform).process_once()
        self.assertEqual(len(platform.handles), 2)
        self.assertTrue(any(q["status"] == "" for q in platform.queries))

    def test_uncertain_result_missing_remote_record_does_not_resubmit(self):
        platform = FakePlatform([alarm("1")])
        platform.mode = "timeout_before"
        self.service(platform).process_once()
        platform.alarms = []
        self.service(platform).process_once()
        self.assertEqual(len(platform.handles), 1)
        self.assertEqual(self.store.get("1")["platform_state"], "uncertain")

    def test_external_handling_is_not_overwritten_and_does_not_generate_mail(self):
        platform = FakePlatform([alarm("1")])
        platform.mode = "timeout_before"
        self.service(platform).process_once()
        platform.alarms[0].update(status=0, isValid=0, handleMethod="人工判断")
        self.service(platform).process_once()
        self.assertEqual(len(platform.handles), 1)
        self.assertEqual(self.store.get("1")["platform_state"], "external")
        self.assertEqual(self.store.counts(), {})

    def test_incomplete_handling_details_stay_uncertain(self):
        platform = FakePlatform([alarm("1")])
        platform.mode = "timeout_after"
        self.service(platform).process_once()
        saved = platform.alarms[0].pop("handleResult")
        self.service(platform).process_once()
        self.assertEqual(self.store.get("1")["platform_state"], "uncertain")
        self.assertEqual(self.store.counts(), {})
        platform.alarms[0]["handleResult"] = saved
        self.service(platform).process_once()
        self.assertEqual(self.store.counts(), {"pending": 2})
        self.assertEqual(len(platform.handles), 1)

    def test_mail_partial_failure_and_persistent_independent_retries(self):
        self.service(FakePlatform([alarm("1")])).process_once()
        mailer = self.mailer()
        mailer.send = Mock(side_effect=[None, OSError("sensitive SMTP response")])
        worker = MailWorker(self.store, mailer, (1, 5, 15, 30), self.logger, self.excel)
        worker.drain_due()
        self.assertEqual(self.store.counts(), {"accepted": 1, "pending": 1})
        self.assertEqual(self.store.get("1")["platform_state"], "handled")
        worker.drain_due()
        self.assertEqual(mailer.send.call_count, 2)
        pending = self.store.due_mail(NOW + 60)[0]
        self.assertEqual(pending["recipient"], "two@unit.test")
        self.assertEqual(pending["last_error"], "OSError")
        original_id = pending["message_id"]
        timestamp = NOW + 60
        for delay in (5, 15, 30, 30):
            with patch("time.time", return_value=timestamp):
                mailer.send = Mock(side_effect=OSError())
                worker.drain_due()
                task = self.store.due_mail(timestamp + delay * 60)[0]
                self.assertEqual(task["next_attempt"], timestamp + delay * 60)
                self.assertEqual(task["message_id"], original_id)
            timestamp += delay * 60
        with patch("time.time", return_value=timestamp):
            mailer.send = Mock()
            MailWorker(StateStore(self.config.state_db_path), mailer, (1, 5, 15, 30), self.logger, self.excel).drain_due()
            self.assertEqual(mailer.send.call_count, 1)
        self.assertEqual(self.store.counts(), {"accepted": 2})

    def test_mail_content_and_single_recipient_envelope(self):
        self.service(FakePlatform([alarm("1")])).process_once()
        mailer = self.mailer()
        mailer.send = Mock()
        MailWorker(self.store, mailer, (1,), self.logger, self.excel).drain_due()
        message, recipient = mailer.send.call_args_list[0].args
        self.assertEqual(message["To"], recipient)
        self.assertNotIn("two@unit.test", str(message))
        body = message.get_content()
        for expected in ("测试船", "人员离岗", "高", "请核实现场", "通知已处理", "系统按配置自动处理", "负责人"):
            self.assertIn(expected, body)

    def test_legacy_ids_are_imported_once_and_original_is_unchanged(self):
        legacy = self.root / "legacy.json"
        content = json.dumps({"processed_ids": ["1", 2]})
        legacy.write_text(content)
        store = StateStore(self.root / "legacy.sqlite3", legacy, started_at=NOW - 60)
        platform = FakePlatform([alarm("1"), alarm("2"), alarm("3")])
        self.service(platform, store=store).process_once()
        self.assertEqual([h["alarm_id"] for h in platform.handles], ["3"])
        self.assertEqual(legacy.read_text(), content)
        self.assertEqual(StateStore(store.path, legacy).get("1")["platform_state"], "legacy")

    def test_corrupt_legacy_state_fails_instead_of_forgetting_ids(self):
        legacy = self.root / "broken.json"
        legacy.write_text("broken")
        with self.assertRaisesRegex(ValueError, "旧版"):
            StateStore(self.root / "broken.sqlite3", legacy)


if __name__ == "__main__":
    unittest.main()
