from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from .client import ApiError, QinSiApiClient
from .config import AppConfig
from .excel_logger import ExcelAlarmLogger
from .store import ProcessedAlarmStore


class AlarmAutoHandleService:
    def __init__(
        self,
        config: AppConfig,
        client: QinSiApiClient,
        store: ProcessedAlarmStore,
        excel_logger: ExcelAlarmLogger,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.excel_logger = excel_logger
        self.logger = logger

    def run_forever(self) -> None:
        self.logger.info("自动处理服务启动，轮询间隔 %s 秒", self.config.poll_interval_seconds)
        self.client.login()
        self.logger.info("登录成功，已获取 accessToken")

        while True:
            started = time.time()
            try:
                self.process_once()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("本轮处理失败: %s", exc)
                self.excel_logger.append_row(
                    event_type="轮询执行",
                    result="失败",
                    message=f"本轮处理失败: {exc}",
                )

            elapsed = time.time() - started
            sleep_seconds = max(1, self.config.poll_interval_seconds - int(elapsed))
            self.logger.debug("本轮结束，%s 秒后进入下一轮", sleep_seconds)
            time.sleep(sleep_seconds)

    def process_once(self) -> None:
        start_time, end_time = self._build_time_range()
        self.logger.info("开始查询未处理告警，时间范围 %s ~ %s", start_time, end_time)
        self.excel_logger.append_row(
            event_type="告警查询",
            result="开始",
            message=f"查询时间范围: {start_time} ~ {end_time}",
        )

        page_num = 1
        total_found = 0
        total_handled = 0

        while True:
            payload = self.client.find_alarms(
                status=self.config.auto_handle.query_status,
                start_time=start_time,
                end_time=end_time,
                page_num=page_num,
                page_size=self.config.page_size,
                order_by=self.config.order_by,
            )

            item = payload.get("item") or {}
            alarms = item.get("list") or []
            total_pages = int(item.get("totalPage") or 1)
            total_found += len(alarms)
            self.logger.info("第 %s/%s 页，查询到 %s 条告警", page_num, total_pages, len(alarms))
            self.excel_logger.append_row(
                event_type="告警查询",
                result="成功",
                message=f"第 {page_num}/{total_pages} 页，查询到 {len(alarms)} 条告警",
            )

            handled_count = self._handle_alarm_list(alarms)
            total_handled += handled_count

            if page_num >= total_pages:
                break
            page_num += 1

        self.logger.info("本轮查询结束，共发现 %s 条告警，成功处理 %s 条", total_found, total_handled)
        self.excel_logger.append_row(
            event_type="轮询汇总",
            result="完成",
            message=f"本轮共发现 {total_found} 条告警，成功处理 {total_handled} 条",
        )

    def _build_time_range(self) -> tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(hours=self.config.lookback_hours)
        return (
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _handle_alarm_list(self, alarms: list[dict]) -> int:
        success_count = 0

        for alarm in alarms:
            alarm_id = str(alarm.get("id", "")).strip()
            if not alarm_id:
                self.logger.warning("跳过一条缺少 id 的告警: %s", alarm)
                self.excel_logger.append_row(
                    event_type="告警处理",
                    result="跳过",
                    message="告警缺少 id，已跳过",
                )
                continue

            ship_name = str(alarm.get("shipName", ""))
            behavior = str(alarm.get("behavior", ""))
            risk_level = str(alarm.get("riskLevel") or alarm.get("alarmLevel") or "")
            happen_time = str(alarm.get("happenTime") or alarm.get("alarmTime") or "")

            if self.store.contains(alarm_id):
                self.logger.info("告警 %s 已处理过，跳过", alarm_id)
                self.excel_logger.append_row(
                    event_type="告警处理",
                    result="跳过",
                    message="该告警已处理过，避免重复提交",
                    alarm_id=alarm_id,
                    ship_name=ship_name,
                    behavior=behavior,
                    risk_level=risk_level,
                    alarm_time=happen_time,
                    handle_method=self.config.auto_handle.handle_method,
                    is_valid=str(self.config.auto_handle.is_valid),
                )
                continue

            self.logger.info(
                "开始处理告警 id=%s, 船名=%s, 行为=%s, 时间=%s",
                alarm_id,
                ship_name,
                behavior,
                happen_time,
            )

            try:
                self.client.handle_alarm(
                    alarm_id=alarm_id,
                    is_valid=self.config.auto_handle.is_valid,
                    handle_method=self.config.auto_handle.handle_method,
                    handle_result=self.config.auto_handle.handle_result,
                    is_archive=self.config.auto_handle.is_archive,
                    remark=self.config.auto_handle.remark,
                )
            except ApiError as exc:
                self.logger.error("处理告警 %s 失败: %s", alarm_id, exc)
                self.excel_logger.append_row(
                    event_type="告警处理",
                    result="失败",
                    message=str(exc),
                    alarm_id=alarm_id,
                    ship_name=ship_name,
                    behavior=behavior,
                    risk_level=risk_level,
                    alarm_time=happen_time,
                    handle_method=self.config.auto_handle.handle_method,
                    is_valid=str(self.config.auto_handle.is_valid),
                )
                continue
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("处理告警 %s 时出现异常: %s", alarm_id, exc)
                self.excel_logger.append_row(
                    event_type="告警处理",
                    result="异常",
                    message=str(exc),
                    alarm_id=alarm_id,
                    ship_name=ship_name,
                    behavior=behavior,
                    risk_level=risk_level,
                    alarm_time=happen_time,
                    handle_method=self.config.auto_handle.handle_method,
                    is_valid=str(self.config.auto_handle.is_valid),
                )
                continue

            self.store.add(alarm_id)
            success_count += 1
            self.logger.info("告警 %s 处理成功", alarm_id)
            self.excel_logger.append_row(
                event_type="告警处理",
                result="成功",
                message="已成功调用处理接口",
                alarm_id=alarm_id,
                ship_name=ship_name,
                behavior=behavior,
                risk_level=risk_level,
                alarm_time=happen_time,
                handle_method=self.config.auto_handle.handle_method,
                is_valid=str(self.config.auto_handle.is_valid),
            )

        return success_count
