from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator

from .client import ApiError, QinSiApiClient
from .config import AppConfig
from .excel_logger import ExcelAlarmLogger
from .store import StateStore
from .time_utils import alarm_timestamp, format_time


class AlarmAutoHandleService:
    def __init__(self, config: AppConfig, client: QinSiApiClient, store: StateStore,
                 excel_logger: ExcelAlarmLogger, logger: logging.Logger,
                 recipients: tuple[str, ...], *, random_value: Callable[[], float] | None = None) -> None:
        self.config, self.client, self.store = config, client, store
        self.excel_logger, self.logger, self.recipients = excel_logger, logger, recipients
        self.random_value = random_value or random.SystemRandom().random

    def event(self, result: str, message: str, alarm: dict | None = None, expected: dict | None = None) -> None:
        snapshot = alarm or {}
        expected = expected or {}
        occurred = alarm_timestamp(snapshot)
        self.logger.info("%s：%s，告警 ID=%s", result, message, snapshot.get("id", ""))
        self.excel_logger.append_row(
            event_type="告警处理", result=result, message=message,
            alarm_id=str(snapshot.get("id") or ""), ship_name=str(snapshot.get("shipName") or ""),
            behavior=str(snapshot.get("behavior") or ""),
            risk_level=str(snapshot.get("riskLevel") or snapshot.get("alarmLevel") or ""),
            alarm_time=format_time(occurred) if occurred is not None else "",
            handle_method=expected.get("handle_method", ""),
            is_valid=str(expected["is_valid"]) if "is_valid" in expected else "",
        )

    def run_forever(self, stop: threading.Event) -> None:
        self.logger.info("服务启动，首次上线时间 %s；系统按配置自动处理", format_time(self.store.deployment_time))
        while not stop.is_set():
            started = time.monotonic()
            try:
                self.process_once()
            except Exception as exc:
                # No raw third-party exception text or response bodies in operational logs.
                self.logger.error("本轮执行失败（%s），下轮继续，查询进度保留", type(exc).__name__)
            stop.wait(max(1, self.config.poll_interval_seconds - (time.monotonic() - started)))

    def _query(self, status: str, start: int, end: int) -> Iterator[dict]:
        page = 1
        while True:
            payload = self.client.find_alarms(
                status=status, start_time=format_time(start), end_time=format_time(end),
                page_num=page, page_size=self.config.page_size, order_by=self.config.order_by,
            )
            item = payload.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("list"), list):
                raise ApiError("报警查询返回结构异常")
            try:
                pages = int(item["totalPage"])
            except (KeyError, ValueError, TypeError):
                raise ApiError("报警查询返回分页信息异常") from None
            if pages < 0 or (pages == 0 and item["list"]):
                raise ApiError("报警查询返回分页信息异常")
            self.logger.info("查询状态=%s，第 %s/%s 页，%s 条", status or "全部", page, pages, len(item["list"]))
            for alarm in item["list"]:
                if not isinstance(alarm, dict):
                    raise ApiError("报警记录结构异常")
                yield alarm
            if page >= pages:
                return
            page += 1

    def _record(self, alarm: dict) -> None:
        value = alarm.get("id")
        alarm_id = str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
        if not alarm_id:
            self.event("保留未处理", "缺少报警 ID", alarm)
            return
        if self.store.get(alarm_id) is not None:
            return
        alarm = dict(alarm, id=alarm_id)
        behavior = alarm.get("behavior")
        occurred = alarm_timestamp(alarm)
        if not isinstance(behavior, str) or not behavior.strip() or occurred is None:
            self.store.record(alarm_id, alarm, "malformed", {}, ())
            self.event("保留未处理", "缺少类型或可解析发生时间", alarm)
            return
        if occurred < self.store.deployment_time:
            self.store.record(alarm_id, alarm, "historical", {}, ())
            self.event("历史报警", "早于首次上线时间，保留原状", alarm)
            return
        if occurred > int(time.time()) + 60:
            self.store.record(alarm_id, alarm, "malformed", {}, ())
            self.event("保留未处理", "报警发生时间超前，请检查平台时间", alarm)
            return
        targeted = any(keyword in behavior for keyword in self.config.keywords)
        decision = ("notify" if self.random_value() < self.config.probability else "leave") if targeted else "invalid"
        auto = self.config.auto_handle
        expected = {
            "is_valid": 1 if decision == "notify" else 0,
            "handle_method": auto.valid_handle_method if decision == "notify" else auto.invalid_handle_method,
            "handle_result": auto.handle_result, "remark": auto.remark, "is_archive": auto.is_archive,
        }
        self.store.record(alarm_id, alarm, decision, expected, self.recipients if decision == "notify" else ())
        self.event("分类完成", {"notify": "抽中通知", "leave": "未抽中，保持未处理", "invalid": "其他类型，按配置标无效"}[decision]
                   + "；系统按配置自动处理", alarm, expected if decision != "leave" else None)

    def process_once(self) -> None:
        end = int(time.time())
        cursor = self.store.metadata("query_cursor")
        start = max(self.store.deployment_time, int(cursor) - 60 if cursor is not None else self.store.deployment_time)
        # Bound each query window while retaining the complete offline interval.
        while start <= end:
            window_end = min(end, start + self.config.lookback_hours * 3600)
            self.logger.info("查询时间范围 %s ~ %s（北京时间）", format_time(start), format_time(window_end))
            for alarm in self._query("2", start, window_end):
                self._record(alarm)
            self.store.advance_cursor(window_end)
            if window_end == end:
                break
            start = window_end
        # No mutation of platform records until every page/window has been saved.
        for row in self.store.actionable():
            try:
                self._process(row)
            except Exception as exc:
                self.logger.error("处理任务失败，保留状态等待恢复，ID=%s（%s）", row["id"], type(exc).__name__)
        counts = self.store.counts()
        self.logger.info("本轮完成，邮件待发送=%s，SMTP 已接受=%s", counts.get("pending", 0), counts.get("accepted", 0))

    def _reconcile(self, row: dict) -> bool:
        occurred = alarm_timestamp(row["snapshot"])
        remote = next((alarm for alarm in self._query("", occurred - 1, occurred + 1)
                       if str(alarm.get("id")) == row["id"]), None)
        if remote is None:
            self.event("结果待核实", "未查询到报警，暂不重复提交", row["snapshot"])
            return False
        status = str(remote.get("status", "")).strip()
        if status in ("2", "未处理", "待处理"):
            return True
        if status not in ("0", "1", "正常处理", "超时处理", "已处理", "已归档"):
            self.event("结果待核实", "平台状态不明确，暂不重复提交", row["snapshot"])
            return False
        expected = row["expected"]
        fields = {"isValid": str(expected["is_valid"]), "handleMethod": expected["handle_method"],
                  "handleResult": expected["handle_result"], "remark": expected["remark"]}
        differs = any(remote.get(key) is not None and str(remote[key]) != value for key, value in fields.items())
        if not differs and any(remote.get(key) is None for key in fields):
            self.event("结果待核实", "平台未返回完整处理详情，暂不重复提交", row["snapshot"])
            return False
        if not differs:
            self.store.complete(row["id"])
            self.event("平台已处理", "已核实上次提交成功；" + ("邮件待发送" if row["decision"] == "notify" else "无需邮件"), row["snapshot"], expected)
        else:
            self.store.set_state(row["id"], "external")
            self.event("保留平台结果", "报警已被处理且与本次任务不一致，不覆盖、不发送邮件", row["snapshot"])
        return False

    def _process(self, row: dict) -> None:
        if (row["platform_state"] == "uncertain" or row["attempts"] > 0) and not self._reconcile(row):
            return
        # Commit before making an external call: a crash must force reconciliation.
        self.store.set_state(row["id"], "uncertain", increment=True)
        try:
            self.client.handle_alarm(alarm_id=row["id"], **row["expected"])
        except ApiError as exc:
            self.store.set_state(row["id"], "uncertain" if exc.outcome_unknown else "pending", str(exc))
            self.event("平台处理失败", str(exc), row["snapshot"], row["expected"])
            return
        self.store.complete(row["id"])
        self.event("平台已处理", "系统按配置自动处理；" + ("邮件待发送" if row["decision"] == "notify" else "无需邮件"), row["snapshot"], row["expected"])
