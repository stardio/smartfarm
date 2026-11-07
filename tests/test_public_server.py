"""공개 웹 대시보드 테스트.

실행:  PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_public_server
운영 DB/상태 파일은 건드리지 않고 임시 폴더를 사용한다.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartfarm.status import write_status
from smartfarm.web.public_server import make_server


class _S:
    def __init__(self, name, type_, simulated=True):
        self.name, self.type, self.simulated = name, type_, simulated
        self.address = "28-secret-address"   # 공개되면 안 되는 값


class _V:
    def __init__(self, name, role, open_):
        self.name, self.role, self.is_open = name, role, open_
        self.pin = 17                         # 공개되면 안 되는 값


CFG = {
    "sampling_interval_sec": 30,
    "rules": {"ph": {"min": 5.5, "max": 6.5}, "temperature": {"max": 30.0},
              "secret": {"min": 1}},
    "telegram_token": "should-not-leak",
}


class PublicServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.db = d / "smartfarm.db"
        cls.status = d / "status.json"

        conn = sqlite3.connect(cls.db)
        conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
                     " sensor TEXT NOT NULL, type TEXT NOT NULL, value REAL NOT NULL)")
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(0, 120):                       # 최근 1시간, 30초 간격
            ts = (now - timedelta(seconds=30 * i)).isoformat()
            rows += [(ts, "temp_1", "temperature", 20.0 + i % 3), (ts, "ph_1", "ph", 6.0)]
        rows.append(((now - timedelta(hours=30)).isoformat(), "temp_1", "temperature", 99.0))  # 24h 밖
        rows.append((now.isoformat(), "x", "secret_type", 1.0))                               # 알 수 없는 타입
        conn.executemany("INSERT INTO readings (ts, sensor, type, value) VALUES (?,?,?,?)", rows)
        conn.commit()
        conn.close()

        write_status(CFG, [_S("temp_1", "temperature", False), _S("ph_1", "ph")],
                     {"temp_1": 21.5, "ph_1": 7.2},
                     [_V("valve_1", "drain", True), _V("valve_2", "irrigation_a", False)],
                     path=cls.status)

        cls.httpd = make_server("127.0.0.1", 0, cls.db, cls.status)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def req(self, method, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_index_and_assets(self):
        for path, ctype in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css")):
            r, body = self.req("GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(ctype, r.getheader("Content-Type"))
            self.assertIn("frame-ancestors 'none'", r.getheader("Content-Security-Policy"))
            self.assertTrue(body)

    def test_status_fields_are_allowlisted(self):
        r, body = self.req("GET", "/api/public/status")
        self.assertEqual(r.status, 200)
        st = json.loads(body)
        self.assertEqual(st["state"], "ok")
        self.assertEqual({s["name"]: s["simulated"] for s in st["sensors"]}, {"temp_1": False, "ph_1": True})
        self.assertEqual(set(st["sensors"][0]), {"name", "type", "value", "simulated"})
        self.assertEqual(set(st["valves"][0]), {"name", "role", "open"})
        self.assertEqual(set(st["rules"]), {"ph", "temperature"})
        text = body.decode()
        for secret in ("28-secret-address", "should-not-leak", '"pin"', "secret"):
            self.assertNotIn(secret, text)

    def test_history_downsampled_and_windowed(self):
        r, body = self.req("GET", "/api/public/history?hours=24")
        self.assertEqual(r.status, 200)
        h = json.loads(body)
        names = {s["name"] for s in h["series"]}
        self.assertEqual(names, {"temp_1", "ph_1"})          # 알 수 없는 타입 제외
        temp = next(s for s in h["series"] if s["name"] == "temp_1")
        self.assertLessEqual(len(temp["points"]), 145)
        self.assertTrue(all(v < 50 for _, v in temp["points"]))   # 30시간 전 값 제외

    def test_invalid_hours_falls_back(self):
        _, body = self.req("GET", "/api/public/history?hours=99999")
        self.assertEqual(json.loads(body)["hours"], 24)
        _, body = self.req("GET", "/api/public/history?hours=abc")
        self.assertEqual(json.loads(body)["hours"], 24)

    def test_unknown_paths_404(self):
        for path in ("/admin/x", "/data/smartfarm.db", "/../.env", "/static/../../.env", "/api/status", "/logs"):
            r, _ = self.req("GET", path)
            self.assertEqual(r.status, 404, path)

    def test_write_methods_rejected(self):
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            r, _ = self.req(m, "/api/public/status")
            self.assertEqual(r.status, 405, m)

    def test_head(self):
        r, body = self.req("HEAD", "/api/v1/health")
        self.assertEqual(r.status, 200)
        self.assertEqual(body, b"")

    def test_db_opened_read_only(self):
        before = self.db.stat().st_mtime_ns
        self.req("GET", "/api/public/history?hours=6")
        self.assertEqual(self.db.stat().st_mtime_ns, before)


class StaleStatusTest(unittest.TestCase):
    def test_stale_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            old = {"updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                   "interval_sec": 30, "sensors": [], "valves": [], "rules": {}}
            (d / "status.json").write_text(json.dumps(old), encoding="utf-8")
            from smartfarm.web.public_server import DataSource
            self.assertEqual(DataSource(d / "none.db", d / "status.json").status()["state"], "stale")
            self.assertEqual(DataSource(d / "none.db", d / "missing.json").status()["state"], "unknown")
            self.assertEqual(DataSource(d / "none.db", d / "missing.json").history(24)["series"], [])


if __name__ == "__main__":
    unittest.main()
