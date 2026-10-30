from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class StateStore:
    """Short transactions and separate connections allow the mail thread to share this store."""

    def __init__(self, path: Path, legacy_path: Path | None = None, *, started_at: int | None = None,
                 account: str | None = None) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alarms (
                    id TEXT PRIMARY KEY,
                    snapshot TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    recipients TEXT NOT NULL,
                    platform_state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alarms_state ON alarms(platform_state);
                CREATE TABLE IF NOT EXISTS mail_tasks (
                    id INTEGER PRIMARY KEY,
                    alarm_id TEXT NOT NULL REFERENCES alarms(id),
                    recipient TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(alarm_id, recipient)
                );
                CREATE INDEX IF NOT EXISTS mail_due ON mail_tasks(state, next_attempt);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('deployment_time', ?)",
                       (str(int(time.time()) if started_at is None else started_at),))
            if account is not None:
                owner = hashlib.sha256(account.encode()).hexdigest()
                existing = db.execute("SELECT value FROM metadata WHERE key='account_hash'").fetchone()
                if existing is not None and existing[0] != owner:
                    raise ValueError("该状态库属于另一个平台账号，请使用独立的数据目录")
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('account_hash', ?)", (owner,))
            if db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone() is None:
                ids = []
                if legacy_path is not None and legacy_path.exists():
                    try:
                        payload = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
                        ids = payload["processed_ids"]
                        if not isinstance(ids, list) or any(not isinstance(v, (str, int)) for v in ids):
                            raise ValueError()
                    except (OSError, ValueError, TypeError, KeyError):
                        raise ValueError("旧版已处理 ID 文件损坏，请恢复后再启动，原文件未修改") from None
                for alarm_id in ids:
                    db.execute("INSERT OR IGNORE INTO alarms "
                               "(id,snapshot,decision,expected,recipients,platform_state,updated_at) "
                               "VALUES (?, '{}', 'legacy', '{}', '[]', 'legacy', ?)",
                               (str(alarm_id), time.time()))
                db.execute("INSERT INTO metadata VALUES ('legacy_imported', '1')")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def metadata(self, key: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    @property
    def deployment_time(self) -> int:
        return int(self.metadata("deployment_time"))

    def advance_cursor(self, timestamp: int) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO metadata VALUES ('query_cursor', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(timestamp),))

    @staticmethod
    def _alarm(row) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("snapshot", "expected", "recipients"):
            value[key] = json.loads(value[key])
        return value

    def get(self, alarm_id: str) -> dict | None:
        with self.connection() as db:
            return self._alarm(db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone())

    def record(self, alarm_id: str, snapshot: dict, decision: str, expected: dict,
               recipients: tuple[str, ...]) -> bool:
        state = "pending" if decision in ("notify", "invalid") else "ignored"
        with self.connection() as db:
            result = db.execute("INSERT OR IGNORE INTO alarms "
                                "(id,snapshot,decision,expected,recipients,platform_state,updated_at) "
                                "VALUES (?,?,?,?,?,?,?)", (
                                    alarm_id, json.dumps(snapshot, ensure_ascii=False), decision,
                                    json.dumps(expected, ensure_ascii=False), json.dumps(recipients), state, time.time(),
                                ))
            return result.rowcount == 1

    def actionable(self) -> list[dict]:
        with self.connection() as db:
            return [self._alarm(row) for row in db.execute(
                "SELECT * FROM alarms WHERE platform_state IN ('pending','uncertain') ORDER BY updated_at,id")]

    def set_state(self, alarm_id: str, state: str, error: str = "", *, increment: bool = False) -> None:
        with self.connection() as db:
            db.execute("UPDATE alarms SET platform_state=?,last_error=?,updated_at=?,attempts=attempts+? WHERE id=?",
                       (state, error, time.time(), int(increment), alarm_id))

    def complete(self, alarm_id: str) -> None:
        # A committed platform result and its outbox entries must never be separated by a crash.
        with self.connection() as db:
            row = self._alarm(db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone())
            db.execute("UPDATE alarms SET platform_state='handled',last_error='',updated_at=? WHERE id=?",
                       (time.time(), alarm_id))
            if row["decision"] == "notify":
                for recipient in row["recipients"]:
                    digest = hashlib.sha256((alarm_id + "\0" + recipient).encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO mail_tasks "
                               "(alarm_id,recipient,message_id,next_attempt) VALUES (?,?,?,?)",
                               (alarm_id, recipient, f"<{digest}@qingsijiankong.local>", time.time()))

    def due_mail(self, timestamp: float, limit: int = 50) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT m.*, a.snapshot, a.expected FROM mail_tasks m JOIN alarms a ON a.id=m.alarm_id "
                              "WHERE m.state='pending' AND m.next_attempt<=? ORDER BY m.next_attempt,m.id LIMIT ?",
                              (timestamp, limit)).fetchall()
            return [dict(row, snapshot=json.loads(row["snapshot"]), expected=json.loads(row["expected"])) for row in rows]

    def mail_result(self, task_id: int, *, accepted: bool, next_attempt: float = 0, error: str = "") -> None:
        with self.connection() as db:
            db.execute("UPDATE mail_tasks SET state=?,attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?",
                       ("accepted" if accepted else "pending", next_attempt, error, task_id))

    def counts(self) -> dict[str, int]:
        with self.connection() as db:
            return {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM mail_tasks GROUP BY state")}
