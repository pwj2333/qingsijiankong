from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook


class ExcelAlarmLogger:
    HEADERS = [
        "记录时间",
        "事件类型",
        "结果",
        "告警ID",
        "船名",
        "异常行为",
        "风险等级",
        "报警时间",
        "处理方式",
        "是否有效",
        "说明",
    ]

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_workbook()

    def _ensure_workbook(self) -> None:
        if self.file_path.exists():
            return

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "处理日志"
        sheet.append(self.HEADERS)
        for index, width in enumerate((20, 14, 14, 24, 18, 26, 12, 20, 16, 12, 50), start=1):
            sheet.column_dimensions[chr(64 + index)].width = width
        workbook.save(self.file_path)

    def append_row(
        self,
        *,
        event_type: str,
        result: str,
        message: str,
        alarm_id: str = "",
        ship_name: str = "",
        behavior: str = "",
        risk_level: str = "",
        alarm_time: str = "",
        handle_method: str = "",
        is_valid: str = "",
    ) -> None:
        workbook = load_workbook(self.file_path)
        sheet = workbook.active
        sheet.append(
            [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                event_type,
                result,
                alarm_id,
                ship_name,
                behavior,
                risk_level,
                alarm_time,
                handle_method,
                is_valid,
                message,
            ]
        )
        workbook.save(self.file_path)
