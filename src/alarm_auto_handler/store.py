from __future__ import annotations

import json
from pathlib import Path


class ProcessedAlarmStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._processed_ids = self._load()

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return set()
        ids = payload.get("processed_ids", [])
        return {str(item) for item in ids}

    def save(self) -> None:
        payload = {"processed_ids": sorted(self._processed_ids)}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def contains(self, alarm_id: str) -> bool:
        return alarm_id in self._processed_ids

    def add(self, alarm_id: str) -> None:
        self._processed_ids.add(alarm_id)
        self.save()
