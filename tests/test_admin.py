"""관리자 로그인/상세 정보 테스트.

실행:  PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_admin
운영 DB/설정/.env 는 건드리지 않고 임시 폴더를 사용한다.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from smartfarm.web import admin
from smartfarm.web.admin_password import write_hash
from smartfarm.web.public_server import make_server

PASSWORD = "correct-horse-battery"
HOST = "127.0.0.1"


def _server(tmp: Path, password_hash):
    db = tmp / "smartfarm.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, ts TEXT, sensor TEXT, type TEXT, value REAL)")
    conn.execute("INSERT INTO readings (ts, sensor, type, value) VALUES ('2026-11-07T00:00:00+00:00','temp_1','temperature',21.0)")
    conn.commit()
    conn.close()
    (tmp / "config.yaml").write_text(
        "sampling_interval_sec: 30\n"
        "sensors:\n  - { name: temp_1, type: temperature, interface: onewire, address: '28-abc' }\n"
        "valves:\n  - { name: valve_1, pin: 17, active_high: false, role: drain }\n"
        "rules:\n  ph: { min: 5.5, max: 6.5 }\n", encoding="utf-8")
    (tmp / "smartfarm.log").write_text(
        "2026-11-07 INFO ok line\n"
        "ERROR url: /bot123456789:AAHsecretsecretsecretsecretsecret/sendMessage failed\n"
        "TELEGRAM_BOT_TOKEN=abcdef\n", encoding="utf-8")
    httpd = make_server(HOST, 0, db, tmp / "status.json", admin_hash=password_hash,
                        config_path=tmp / "config.yaml", log_path=tmp / "smartfarm.log")
    httpd.RequestHandlerClass.auth.FAIL_DELAY_SEC = 0     # 테스트 속도
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = _server(Path(self.tmp.name), admin.hash_password(PASSWORD))
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, origin="same"):
        c = http.client.HTTPConnection(HOST, self.port, timeout=10)
        headers = {}
        if origin == "same":
            headers["Origin"] = f"http://{HOST}:{self.port}"
        elif origin:
            headers["Origin"] = origin
        if cookie:
            headers["Cookie"] = cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            j = json.loads(raw) if raw else None
        except ValueError:
            j = None
        return r, j

    def login(self, pw=PASSWORD):
        r, j = self.req("POST", "/admin/api/login", {"password": pw})
        cookie = r.getheader("Set-Cookie")
        return r, j, (cookie.split(";", 1)[0] if cookie else None)

    def test_admin_page_served(self):
        r, _ = self.req("GET", "/admin")
        self.assertEqual(r.status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))

    def test_overview_requires_login(self):
        r, j = self.req("GET", "/admin/api/overview")
        self.assertEqual(r.status, 401)
        self.assertEqual(j, {"error": "login_required"})

    def test_wrong_password(self):
        r, j, cookie = self.login("wrong-password")
        self.assertEqual(r.status, 401)
        self.assertIsNone(cookie)

    def test_login_overview_logout(self):
        r, _, cookie = self.login()
        self.assertEqual(r.status, 200)
        set_cookie = r.getheader("Set-Cookie")
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, set_cookie)
        self.assertTrue(cookie.startswith("__Host-sf_admin="))

        r, j = self.req("GET", "/admin/api/session", cookie=cookie)
        self.assertTrue(j["logged_in"])

        r, j = self.req("GET", "/admin/api/overview", cookie=cookie)
        self.assertEqual(r.status, 200)
        self.assertEqual(j["config"]["sensors"][0]["address"], "28-abc")
        self.assertEqual(j["config"]["valves"][0]["pin"], 17)
        self.assertEqual(j["db"]["rows"], 1)
        self.assertIn("smartfarm", j["services"])
        logs = "\n".join(j["logs"])
        self.assertIn("ok line", logs)
        self.assertNotIn("AAHsecret", logs)
        self.assertNotIn("abcdef", logs)

        r, _ = self.req("POST", "/admin/api/logout", {}, cookie=cookie)
        self.assertEqual(r.status, 200)
        r, _ = self.req("GET", "/admin/api/overview", cookie=cookie)
        self.assertEqual(r.status, 401)

    def test_forged_cookie_rejected(self):
        r, _ = self.req("GET", "/admin/api/overview", cookie="__Host-sf_admin=forged")
        self.assertEqual(r.status, 401)

    def test_cross_origin_post_rejected(self):
        for origin in ("https://evil.example", None):
            r, j = self.req("POST", "/admin/api/login", {"password": PASSWORD}, origin=origin)
            self.assertEqual(r.status, 403, origin)
            self.assertIsNone(r.getheader("Set-Cookie"))

    def test_body_too_large(self):
        r, _ = self.req("POST", "/admin/api/login", {"password": "x" * 5000})
        self.assertEqual(r.status, 413)

    def test_lockout_after_repeated_failures(self):
        for _ in range(admin.AdminAuth.FAIL_LIMIT - 1):
            r, _, _ = self.login("nope-nope-nope")
            self.assertEqual(r.status, 401)
        r, j, _ = self.login("nope-nope-nope")
        self.assertEqual(r.status, 429)
        # 잠금 중에는 올바른 비밀번호도 거부
        r, j, cookie = self.login()
        self.assertEqual(r.status, 429)
        self.assertIsNone(cookie)
        self.assertGreater(j["locked_sec"], 0)


class NotConfiguredTest(unittest.TestCase):
    def test_login_disabled_without_hash(self):
        with tempfile.TemporaryDirectory() as d:
            httpd = _server(Path(d), None)
            try:
                port = httpd.server_address[1]
                c = http.client.HTTPConnection(HOST, port, timeout=5)
                c.request("POST", "/admin/api/login", body=b'{"password":"anything"}',
                          headers={"Origin": f"http://{HOST}:{port}", "Content-Type": "application/json"})
                self.assertEqual(c.getresponse().status, 503)
                c.close()
                c = http.client.HTTPConnection(HOST, port, timeout=5)
                c.request("GET", "/admin/api/session")
                self.assertFalse(json.loads(c.getresponse().read())["configured"])
                c.close()
            finally:
                httpd.shutdown()
                httpd.server_close()


class PasswordHashTest(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = admin.hash_password(PASSWORD)
        self.assertNotIn(PASSWORD, h)
        self.assertTrue(admin.verify_password(PASSWORD, h))
        self.assertFalse(admin.verify_password("other", h))
        self.assertFalse(admin.verify_password(PASSWORD, None))
        self.assertFalse(admin.verify_password(PASSWORD, "garbage"))
        self.assertNotEqual(h, admin.hash_password(PASSWORD))   # salt 다름

    def test_write_hash_keeps_other_env_lines(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("TELEGRAM_BOT_TOKEN=x\nADMIN_PASSWORD_HASH=old\nLOG_LEVEL=INFO\n", encoding="utf-8")
            write_hash(env, "scrypt:new")
            text = env.read_text(encoding="utf-8")
            self.assertIn("TELEGRAM_BOT_TOKEN=x", text)
            self.assertIn("LOG_LEVEL=INFO", text)
            self.assertIn("ADMIN_PASSWORD_HASH=scrypt:new", text)
            self.assertNotIn("=old", text)
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)



class PinTest(unittest.TestCase):
    def test_secret_kind(self):
        self.assertEqual(admin.secret_kind("4827"), "pin")
        self.assertEqual(admin.secret_kind("a-long-password"), "password")
        for bad in ("123", "12345", "12a4", "short", "", "４８２７"):   # 전각 숫자도 거부
            self.assertIsNone(admin.secret_kind(bad), bad)

    def test_pin_login_and_session_flag(self):
        with tempfile.TemporaryDirectory() as d:
            httpd = _server(Path(d), admin.hash_password("4827"))
            httpd.RequestHandlerClass.auth.kind = "pin"
            try:
                port = httpd.server_address[1]
                c = http.client.HTTPConnection(HOST, port, timeout=5)
                c.request("GET", "/admin/api/session")
                self.assertTrue(json.loads(c.getresponse().read())["pin"])
                c.close()
                c = http.client.HTTPConnection(HOST, port, timeout=5)
                c.request("POST", "/admin/api/login", body=b'{"password":"4827"}',
                          headers={"Origin": f"http://{HOST}:{port}", "Content-Type": "application/json"})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertIn("__Host-sf_admin=", r.getheader("Set-Cookie"))
                c.close()
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_lock_duration_escalates_and_caps(self):
        auth = admin.AdminAuth(admin.hash_password("4827"), "pin")
        auth.FAIL_DELAY_SEC = 0
        durations = []
        for _ in range(9):
            for _ in range(auth.FAIL_LIMIT):
                auth.login("0000")
            durations.append(auth.locked_for())
            auth._locked_until = 0          # 잠금 시간 경과를 흉내
        base = auth.LOCK_SEC
        self.assertAlmostEqual(durations[0], base, delta=2)
        self.assertAlmostEqual(durations[1], base * 2, delta=2)
        self.assertAlmostEqual(durations[2], base * 4, delta=2)
        self.assertAlmostEqual(durations[-1], auth.LOCK_MAX_SEC, delta=2)
        # 성공하면 단계 초기화
        self.assertIsNotNone(auth.login("4827"))
        for _ in range(auth.FAIL_LIMIT):
            auth.login("0000")
        self.assertAlmostEqual(auth.locked_for(), base, delta=2)

    def test_write_hash_stores_kind(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            write_hash(env, "scrypt:a", "pin")
            write_hash(env, "scrypt:b", "pin")
            text = env.read_text(encoding="utf-8")
            self.assertEqual(text.count("ADMIN_PASSWORD_HASH="), 1)
            self.assertIn("ADMIN_PASSWORD_HASH=scrypt:b", text)
            self.assertIn("ADMIN_PASSWORD_KIND=pin", text)


if __name__ == "__main__":
    unittest.main()
