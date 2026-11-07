"""텔레그램 봇 명령 테스트 (네트워크 없음).

실행:  PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_bot
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartfarm.core.envfile import is_placeholder, update_env
from smartfarm.notify import bot, commands
from smartfarm.notify.telegram import parse_chat_ids, read_settings, redact

NOW = datetime(2026, 11, 7, 6, 0, 0, tzinfo=timezone.utc)
RULES = {"temperature": {"max": 30.0}, "ph": {"min": 5.5, "max": 6.5}}
SYSTEM = {"cpu_temp_c": 48.2, "uptime_sec": 93784, "mem_available": 500 << 20,
          "disk_free": 20 << 30}
SERVICES_OK = {"smartfarm": "active", "smartfarm-web": "active", "smartfarm-bot": "active"}


def status(age_sec=5, temp1=22.43, ph=6.0):
    return {
        "updated_at": (NOW - timedelta(seconds=age_sec)).isoformat(),
        "interval_sec": 10,
        "sensors": [
            {"name": "temp_1", "type": "temperature", "value": temp1, "simulated": False},
            {"name": "temp_2", "type": "temperature", "value": 21.0, "simulated": False},
            {"name": "ph_1", "type": "ph", "value": ph, "simulated": True},
        ],
        "valves": [{"name": "valve_1", "role": "drain", "open": False}],
    }


class CommandTest(unittest.TestCase):
    def test_parse_command(self):
        self.assertEqual(commands.parse_command("/temp"), "temp")
        self.assertEqual(commands.parse_command("/Status@FarmBot", "farmbot"), "status")
        self.assertIsNone(commands.parse_command("/status@OtherBot", "FarmBot"))
        self.assertIsNone(commands.parse_command("hello"))
        self.assertIsNone(commands.parse_command(None))

    def test_help(self):
        for c in ("/status", "/temp", "/alerts", "/help"):
            self.assertIn(c, commands.cmd_help())
        self.assertIn("알 수 없는 명령", commands.reply_for("nope"))

    def test_temp(self):
        t = commands.cmd_temp(status(), RULES, {"temp_1": (20.1, 23.0)}, now=NOW)
        self.assertIn("temp_1: 22.4 °C", t)
        self.assertIn("24시간 20.1 °C ~ 23.0 °C", t)
        self.assertIn("temp_2: 21.0 °C", t)
        self.assertNotIn("ph_1", t)
        self.assertNotIn("⚠", t)
        self.assertIn("최대 30.0 °C", t)

    def test_temp_breach_and_stale(self):
        t = commands.cmd_temp(status(age_sec=600, temp1=31.2), RULES, {}, now=NOW)
        self.assertIn("31.2 °C ⚠", t)
        self.assertIn("갱신 중단", t)

    def test_status(self):
        s = commands.cmd_status(status(), RULES, SYSTEM, SERVICES_OK, now=NOW)
        self.assertIn("측정: 정상 (10초 주기)", s)
        self.assertIn("ph_1: 6.00 (모의값)", s)
        self.assertIn("valve_1 (배수): ⚪ 닫힘", s)
        self.assertIn("측정 서버: ✅ 실행 중", s)
        self.assertIn("CPU 온도 48.2 °C", s)
        self.assertIn("가동 1일 2시간", s)

    def test_status_without_data(self):
        s = commands.cmd_status({}, RULES, SYSTEM, SERVICES_OK, now=NOW)
        self.assertIn("측정 데이터", s)

    def test_alerts_none(self):
        a = commands.cmd_alerts(status(), RULES, SYSTEM, SERVICES_OK, 0, now=NOW)
        self.assertIn("현재 경고 없음", a)

    def test_alerts_many(self):
        sv = dict(SERVICES_OK, **{"smartfarm-web": "failed"})
        sysinfo = dict(SYSTEM, cpu_temp_c=80.0, disk_free=100 << 20)
        a = commands.cmd_alerts(status(temp1=31.0, ph=7.1), RULES, sysinfo, sv, 3, now=NOW)
        self.assertIn("temp_1 온도 31.0 °C — 최대 30.0 °C 초과", a)
        self.assertIn("ph_1 pH 7.10 — 최대 6.50 초과 (모의값)", a)
        self.assertIn("웹 서비스 failed", a)
        self.assertIn("CPU 온도 높음", a)
        self.assertIn("디스크 여유 부족", a)
        self.assertIn("로그 오류 3건", a)
        self.assertIn("경고 6건", a)

    def test_temp_range_from_db(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "x.db"
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, ts TEXT, sensor TEXT, type TEXT, value REAL)")
            rows = [((NOW - timedelta(hours=h)).isoformat(), "temp_1", "temperature", v)
                    for h, v in ((1, 20.0), (2, 25.5), (30, 99.0))]
            rows.append(((NOW - timedelta(hours=1)).isoformat(), "ph_1", "ph", 6.0))
            c.executemany("INSERT INTO readings (ts, sensor, type, value) VALUES (?,?,?,?)", rows)
            c.commit()
            c.close()
            self.assertEqual(commands.temp_range_24h(db, NOW), {"temp_1": (20.0, 25.5)})
            self.assertEqual(commands.temp_range_24h(Path(d) / "missing.db", NOW), {})

    def test_recent_log_errors(self):
        now = datetime(2026, 11, 7, 15, 0, 0)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.log"
            p.write_text(
                "2026-11-07 14:30:00,000 ERROR   x: recent\n"
                "2026-11-07 14:31:00,000 INFO    x: info\n"
                "Traceback ERROR line without time\n"
                "2026-11-07 12:00:00,000 ERROR   x: old\n", encoding="utf-8")
            self.assertEqual(commands.recent_log_errors(p, 1, now), 1)


class BotTest(unittest.TestCase):
    def upd(self, chat_id=111, text="/help", age=1):
        return {"update_id": 5, "message": {"chat": {"id": chat_id}, "text": text,
                                            "date": int(time.time()) - age}}

    def test_authorized_reply(self):
        res = bot.handle_update(self.upd(), {111}, "FarmBot", time.time())
        self.assertEqual(res[0], 111)
        self.assertIn("/temp", res[1])

    def test_unauthorized_gets_only_chat_id(self):
        res = bot.handle_update(self.upd(chat_id=999, text="/status"), {111}, "FarmBot", time.time())
        self.assertEqual(res[0], 999)
        self.assertIn("등록되지 않은", res[1])
        self.assertIn("999", res[1])
        self.assertNotIn("°C", res[1])

    def test_ignored(self):
        now = time.time()
        self.assertIsNone(bot.handle_update(self.upd(text="hi"), {111}, "FarmBot", now))
        self.assertIsNone(bot.handle_update(self.upd(age=600), {111}, "FarmBot", now))
        self.assertIsNone(bot.handle_update({"update_id": 1}, {111}, "FarmBot", now))


class SettingsTest(unittest.TestCase):
    def test_placeholder_is_unset(self):
        self.assertTrue(is_placeholder("여기에_봇_토큰"))
        self.assertTrue(is_placeholder(""))
        token, ids = read_settings({"TELEGRAM_BOT_TOKEN": "여기에_봇_토큰", "TELEGRAM_CHAT_ID": "여기에_chat_id"})
        self.assertIsNone(token)
        self.assertEqual(ids, [])

    def test_chat_ids(self):
        self.assertEqual(parse_chat_ids("123, -100456 ,abc"), [123, -100456])

    def test_redact(self):
        s = redact("for url: https://api.telegram.org/bot123456789:AAHabcdefghijklmnopqrstuvwxyz12/getMe")
        self.assertNotIn("AAH", s)

    def test_update_env(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("TELEGRAM_BOT_TOKEN=old\nTELEGRAM_CHAT_ID=old\nLOG_LEVEL=INFO\n", encoding="utf-8")
            update_env(env, {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "42"})
            text = env.read_text(encoding="utf-8")
            self.assertEqual(text, "TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=42\nLOG_LEVEL=INFO\n")
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
