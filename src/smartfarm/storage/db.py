"""SQLite 저장소: 센서 측정값 기록."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..core.logging import get_logger

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


class Database:
    def __init__(self, cfg: dict | None = None):
        self.path = DATA_DIR / "smartfarm.db"
        self.conn = sqlite3.connect(self.path)
        self._init_schema()
        log.info("DB 연결: %s", self.path)

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                sensor    TEXT NOT NULL,
                type      TEXT NOT NULL,
                value     REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)"
        )
        self.conn.commit()

    def save_reading(self, sensor: str, type_: str, value: float) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO readings (ts, sensor, type, value) VALUES (?, ?, ?, ?)",
            (ts, sensor, type_, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
