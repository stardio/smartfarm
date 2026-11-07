"""data/control.db — 제어 설정·명령·운전 기록.

두 프로세스가 함께 쓴다(WAL 모드):
  웹서버(smartfarm-web) : 구역 설정 저장, 명령 넣기, 리포트 조회
  측정 서버(smartfarm)  : 설정 읽기, 명령 처리, 운전 기록(runs) 쓰기

테이블
  zone_config(system, zone, data, updated_at)   구역 설정(JSON). nutrient zone 0 = 자동 투입 설정
  meta(key, value)                              config_version: 설정이 바뀔 때마다 +1
  commands(id, ts, source, payload, status, result, done_ts)   웹 → 제어기 명령 대기열
  runs(id, system, zone, start_ts, end_ts, duration_sec, trigger, result, liters)   운전 기록(리포트)
  events(id, ts, source, action, detail)        설정 변경·명령 감사 기록
시각은 모두 epoch 초(REAL).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONTROL_DB = ROOT / "data" / "control.db"
COMMAND_TTL_SEC = 60        # 이보다 오래된 명령은 실행하지 않음(제어기가 꺼져 있던 동안 쌓인 명령)

SCHEMA = """
CREATE TABLE IF NOT EXISTS zone_config (
  system TEXT NOT NULL, zone INTEGER NOT NULL, data TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY (system, zone));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', result TEXT, done_ts REAL);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, system TEXT NOT NULL, zone INTEGER NOT NULL,
  start_ts REAL NOT NULL, end_ts REAL, duration_sec REAL, trigger TEXT NOT NULL,
  result TEXT, liters REAL);
CREATE INDEX IF NOT EXISTS idx_runs_start ON runs(start_ts);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class ControlStore:
    def __init__(self, path: Path = CONTROL_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _run(self, fn):
        conn = self._connect()
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    # ── 설정 ───────────────────────────────────────────────
    def config_version(self) -> int:
        row = self._run(lambda c: c.execute("SELECT value FROM meta WHERE key='config_version'").fetchone())
        return int(row[0]) if row else 0

    def all_zone_configs(self) -> dict[tuple[str, int], dict]:
        rows = self._run(lambda c: c.execute("SELECT system, zone, data FROM zone_config").fetchall())
        out = {}
        for system, zone, data in rows:
            try:
                out[(system, zone)] = json.loads(data)
            except ValueError:
                continue
        return out

    def save_zone_config(self, system: str, zone: int, data: dict, source: str = "web") -> None:
        now = time.time()

        def fn(c):
            c.execute("INSERT INTO zone_config (system, zone, data, updated_at) VALUES (?,?,?,?) "
                      "ON CONFLICT(system, zone) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                      (system, zone, json.dumps(data, ensure_ascii=False), now))
            c.execute("INSERT INTO meta (key, value) VALUES ('config_version', '1') "
                      "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
            c.execute("INSERT INTO events (ts, source, action, detail) VALUES (?,?,?,?)",
                      (now, source, "config", f"{system}/{zone}"))
        self._run(fn)

    # ── 명령 ───────────────────────────────────────────────
    def add_command(self, payload: dict, source: str = "web") -> int:
        now = time.time()

        def fn(c):
            cur = c.execute("INSERT INTO commands (ts, source, payload) VALUES (?,?,?)",
                            (now, source, json.dumps(payload, ensure_ascii=False)))
            c.execute("INSERT INTO events (ts, source, action, detail) VALUES (?,?,?,?)",
                      (now, source, "command", json.dumps(payload, ensure_ascii=False)))
            return cur.lastrowid
        return self._run(fn)

    def pending_commands(self, now: float | None = None) -> list[tuple[int, float, dict]]:
        now = now or time.time()

        def fn(c):
            c.execute("UPDATE commands SET status='expired', done_ts=? WHERE status='pending' AND ts < ?",
                      (now, now - COMMAND_TTL_SEC))
            return c.execute("SELECT id, ts, payload FROM commands WHERE status='pending' ORDER BY id").fetchall()
        out = []
        for cid, ts, payload in self._run(fn):
            try:
                out.append((cid, ts, json.loads(payload)))
            except ValueError:
                self.finish_command(cid, "error", "형식 오류")
        return out

    def finish_command(self, cid: int, status: str, result: str = "") -> None:
        self._run(lambda c: c.execute("UPDATE commands SET status=?, result=?, done_ts=? WHERE id=?",
                                      (status, result[:200], time.time(), cid)))

    def command_result(self, cid: int) -> dict | None:
        row = self._run(lambda c: c.execute("SELECT status, result FROM commands WHERE id=?", (cid,)).fetchone())
        return {"status": row[0], "result": row[1] or ""} if row else None

    # ── 운전 기록 ──────────────────────────────────────────
    def start_run(self, system: str, zone: int, start_ts: float, trigger: str) -> int:
        return self._run(lambda c: c.execute(
            "INSERT INTO runs (system, zone, start_ts, trigger) VALUES (?,?,?,?)",
            (system, zone, start_ts, trigger)).lastrowid)

    def end_run(self, run_id: int, end_ts: float, result: str, flow_lpm: float = 0.0) -> None:
        def fn(c):
            row = c.execute("SELECT start_ts FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return
            dur = max(0.0, end_ts - row[0])
            liters = round(dur / 60 * flow_lpm, 1) if flow_lpm else None
            c.execute("UPDATE runs SET end_ts=?, duration_sec=?, result=?, liters=? WHERE id=?",
                      (end_ts, round(dur, 1), result, liters, run_id))
        self._run(fn)

    def close_open_runs(self, now: float, result: str = "interrupted") -> int:
        """이전 실행에서 끝나지 않은 기록 정리(전원 차단·재시작). 정리한 개수."""
        def fn(c):
            rows = c.execute("SELECT id, start_ts FROM runs WHERE end_ts IS NULL").fetchall()
            for rid, st in rows:
                c.execute("UPDATE runs SET end_ts=?, duration_sec=?, result=? WHERE id=?",
                          (st, 0, result, rid))
            return len(rows)
        return self._run(fn)

    def log_event(self, action: str, detail: str = "", source: str = "controller",
                  ts: float | None = None) -> None:
        self._run(lambda c: c.execute("INSERT INTO events (ts, source, action, detail) VALUES (?,?,?,?)",
                                      (ts or time.time(), source, action, detail[:500])))

    def doses_today(self, now: float) -> int:
        start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = self._run(lambda c: c.execute(
            "SELECT COUNT(*) FROM events WHERE action='dose' AND ts>=?", (start,)).fetchone())
        return int(row[0])

    # ── 리포트 (웹) ────────────────────────────────────────
    def recent_runs(self, limit: int = 100, system: str | None = None) -> list[dict]:
        q = "SELECT system, zone, start_ts, end_ts, duration_sec, trigger, result, liters FROM runs"
        args: list = []
        if system:
            q += " WHERE system=?"
            args.append(system)
        q += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        rows = self._run(lambda c: c.execute(q, args).fetchall())
        keys = ("system", "zone", "start_ts", "end_ts", "duration_sec", "trigger", "result", "liters")
        return [dict(zip(keys, r)) for r in rows]

    def daily_report(self, days: int, now: float | None = None) -> dict:
        """최근 days 일 동안 날짜·시스템·구역별 운전 횟수, 총 시간(초), 사용량(L)."""
        now = now or time.time()
        today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        first = today - timedelta(days=days - 1)
        rows = self._run(lambda c: c.execute(
            "SELECT system, zone, date(start_ts, 'unixepoch', 'localtime') AS d, COUNT(*), "
            "SUM(COALESCE(duration_sec, 0)), SUM(COALESCE(liters, 0)) FROM runs "
            "WHERE start_ts >= ? AND end_ts IS NOT NULL GROUP BY system, zone, d ORDER BY d",
            (first.timestamp(),)).fetchall())
        dates = [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        return {
            "days": days, "dates": dates,
            "rows": [{"system": s, "zone": z, "date": d, "count": n, "seconds": round(sec or 0),
                      "liters": round(l or 0, 1)} for s, z, d, n, sec, l in rows],
        }
