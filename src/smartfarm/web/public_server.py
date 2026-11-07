"""공개(보기 전용) 웹 대시보드 서버.

구조 (RTK Base 공개 게이트웨이와 동일한 방식):
    인터넷 → Tailscale Funnel(HTTPS) → 127.0.0.1:8081 (이 서버)

- 표준 라이브러리만 사용. 루프백 주소에서만 수신한다.
- 공개 경로는 GET/HEAD 만 허용(보기 전용).
- /control: 관리자 로그인 후 구역 제어(관수·양액·조명·공조팬·히터). 웹서버는 출력을 직접
  건드리지 않고 data/control.db 명령 대기열에 넣는다(control_api.py).
- /admin: 관리자 비밀번호 로그인 후 상세 정보(설정·시스템·로그) 조회. POST 는
  /admin/api/login, /admin/api/logout 두 개뿐이며 Origin 이 같은 사이트일 때만 받는다.
- 데이터 출처: data/status.json(메인 서버가 기록), data/smartfarm.db(읽기 전용으로 열기).
- 응답 필드는 허용 목록으로 새로 구성한다(핀·주소·로그·설정은 내보내지 않음).

실행:
    PYTHONPATH=src .venv/bin/python -m smartfarm.web.public_server --port 8081
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlsplit

from . import admin as admin_mod
from .control_api import REPORT_DAYS, ControlAPI

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "data" / "smartfarm.db"
DEFAULT_STATUS = ROOT / "data" / "status.json"
DEFAULT_CONFIG = ROOT / "config" / "config.yaml"
DEFAULT_LOG = ROOT / "logs" / "smartfarm.log"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_CONTROL_DB = ROOT / "data" / "control.db"
DEFAULT_CONTROL_STATUS = ROOT / "data" / "control.json"
MAX_BODY = 2048
MAX_CONTROL_BODY = 16384
CONTROL_POSTS = ("/admin/api/control/command", "/admin/api/control/config")
STATIC_DIR = Path(__file__).resolve().parent / "static"

SENSOR_TYPES = ("temperature", "humidity", "ph", "ec")
RULE_KEYS = ("min", "max")
HISTORY_HOURS = (6, 24, 72)
HISTORY_POINTS = 144          # 기간과 무관하게 센서당 최대 점 개수
STATUS_CACHE_SEC = 2
HISTORY_CACHE_SEC = 30
STALE_FACTOR = 3              # 측정 주기 × 이 값 이상 갱신이 없으면 '중단'으로 표시

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/admin/": ("admin.html", "text/html; charset=utf-8"),
    "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/control": ("control.html", "text/html; charset=utf-8"),
    "/control.js": ("control.js", "text/javascript; charset=utf-8"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _num(v):
    """숫자만 통과(그 외 None). bool 은 숫자로 취급하지 않는다."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class DataSource:
    """status.json / DB 를 읽어 공개용 객체로 재구성. 짧게 캐시한다."""

    def __init__(self, db_path: Path, status_path: Path):
        self.db_path = Path(db_path)
        self.status_path = Path(status_path)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}

    def _cached(self, key: str, ttl: float, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    # ── 현재 상태 ───────────────────────────────────────────
    def status(self) -> dict:
        return self._cached("status", STATUS_CACHE_SEC, self._load_status)

    def _load_status(self) -> dict:
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "unknown", "updated_at": None, "sensors": [], "valves": [], "rules": {}}

        updated = _parse_ts(raw.get("updated_at"))
        interval = _num(raw.get("interval_sec")) or 30
        if updated is None:
            state = "unknown"
        else:
            age = (datetime.now(timezone.utc) - updated).total_seconds()
            state = "ok" if age <= interval * STALE_FACTOR else "stale"

        sensors = []
        for s in raw.get("sensors") or []:
            if not isinstance(s, dict) or s.get("type") not in SENSOR_TYPES:
                continue
            sensors.append({
                "name": str(s.get("name", ""))[:32],
                "type": s["type"],
                "value": _num(s.get("value")),
                "simulated": bool(s.get("simulated", True)),
            })

        valves = []
        for v in raw.get("valves") or []:
            if not isinstance(v, dict):
                continue
            valves.append({
                "name": str(v.get("name", ""))[:32],
                "role": str(v.get("role", ""))[:32],
                "open": bool(v.get("open", False)),
            })

        rules = {}
        for t, r in (raw.get("rules") or {}).items():
            if t in SENSOR_TYPES and isinstance(r, dict):
                clean = {k: _num(r.get(k)) for k in RULE_KEYS if _num(r.get(k)) is not None}
                if clean:
                    rules[t] = clean

        return {
            "state": state,
            "updated_at": updated.isoformat() if updated else None,
            "interval_sec": interval,
            "sensors": sensors,
            "valves": valves,
            "rules": rules,
        }

    # ── 이력(그래프) ─────────────────────────────────────────
    def history(self, hours: int) -> dict:
        return self._cached(f"history:{hours}", HISTORY_CACHE_SEC, lambda: self._load_history(hours))

    def _load_history(self, hours: int) -> dict:
        bucket = max(60, hours * 3600 // HISTORY_POINTS)   # 초
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        series: dict[str, dict] = {}
        try:
            # mode=ro: 웹서버가 DB 를 절대 수정하지 못하게 읽기 전용으로 연다.
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return {"hours": hours, "bucket_sec": bucket, "series": []}
        try:
            rows = conn.execute(
                """
                SELECT sensor, type,
                       (CAST(strftime('%s', ts) AS INTEGER) / ?) * ? AS b,
                       AVG(value)
                FROM readings
                WHERE ts >= ?
                GROUP BY sensor, type, b
                ORDER BY sensor, b
                """,
                (bucket, bucket, since),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()

        for sensor, type_, b, avg in rows:
            if type_ not in SENSOR_TYPES or b is None or avg is None:
                continue
            s = series.setdefault(sensor, {"name": str(sensor)[:32], "type": type_, "points": []})
            s["points"].append([int(b), round(float(avg), 3)])
        return {"hours": hours, "bucket_sec": bucket, "series": list(series.values())}


class Handler(BaseHTTPRequestHandler):
    server_version = "smartfarm-web"
    sys_version = ""
    data: DataSource            # make_server 에서 주입
    auth: admin_mod.AdminAuth
    paths: dict
    control: ControlAPI

    # 접근 로그는 남기지 않는다(공개 서버, journal 용량 절약)
    def log_message(self, format, *args):  # noqa: A002
        return

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, obj: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: dict) -> None:
        self._send_json(HTTPStatus.OK, obj)

    # ── 관리자 공통 ──────────────────────────────────────────
    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = SimpleCookie(raw)
        except CookieError:
            return None
        m = c.get(self.auth.COOKIE)
        return m.value if m else None

    def _is_admin(self) -> bool:
        return self.auth.check(self._session_token())

    def _same_origin(self) -> bool:
        """POST 는 같은 사이트에서 온 요청만 허용(CSRF 방지)."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if not origin or not host:
            return False
        return urlsplit(origin).netloc == host

    def _cookie(self, value: str, max_age: int) -> str:
        return (f"{self.auth.COOKIE}={value}; Path=/; Max-Age={max_age}; "
                "HttpOnly; Secure; SameSite=Strict")

    def _not_found(self) -> None:
        self._send(HTTPStatus.NOT_FOUND, b"Not Found", "text/plain; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        path = url.path

        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            try:
                body = (STATIC_DIR / name).read_bytes()
            except OSError:
                return self._not_found()
            return self._send(HTTPStatus.OK, body, ctype, "public, max-age=60")

        if path == "/api/public/status":
            return self._json(self.data.status())

        if path == "/api/public/history":
            q = parse_qs(url.query)
            try:
                hours = int(q.get("hours", ["24"])[0])
            except ValueError:
                hours = 24
            if hours not in HISTORY_HOURS:
                hours = 24
            return self._json(self.data.history(hours))

        if path == "/api/public/control":
            return self._json(self.control.public())

        if path == "/api/v1/health":
            st = self.data.status()
            return self._json({"state": st["state"], "updated_at": st["updated_at"]})

        if path == "/admin/api/session":
            return self._json({"configured": self.auth.configured,
                               "pin": self.auth.kind == "pin",
                               "logged_in": self._is_admin(),
                               "locked_sec": self.auth.locked_for()})

        if path == "/admin/api/overview":
            if not self._is_admin():
                return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "login_required"})
            p = self.paths
            data = admin_mod.overview(p["root"], p["db"], p["config"], p["log"])
            data["status"] = self.data.status()
            return self._json(data)

        if path.startswith("/admin/api/control/"):
            if not self._is_admin():
                return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "login_required"})
            q = parse_qs(url.query)
            try:
                days = int(q.get("days", ["7"])[0])
            except ValueError:
                days = 7
            days = days if days in REPORT_DAYS else 7
            if path == "/admin/api/control/state":
                return self._json(self.control.admin_state(self.auth.kind == "pin"))
            if path == "/admin/api/control/report":
                return self._json(self.control.report(days))
            if path == "/admin/api/control/report.csv":
                body = self.control.report_csv(days)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="smartfarm-report-{days}d.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return None

        return self._not_found()

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path not in ("/admin/api/login", "/admin/api/logout") + CONTROL_POSTS:
            return self._method_not_allowed()
        if not self._same_origin():
            return self._send_json(HTTPStatus.FORBIDDEN, {"error": "bad_origin"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        limit = MAX_CONTROL_BODY if path in CONTROL_POSTS else MAX_BODY
        if length < 0 or length > limit:
            return self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
        raw = self.rfile.read(length) if length else b""

        if path in CONTROL_POSTS:
            return self._control_post(path, raw)

        if path == "/admin/api/logout":
            self.auth.logout(self._session_token())
            return self._send_json(HTTPStatus.OK, {"ok": True},
                                   {"Set-Cookie": self._cookie("", 0)})

        if not self.auth.configured:
            return self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "not_configured"})
        locked = self.auth.locked_for()
        if locked:
            return self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "locked", "locked_sec": locked})
        try:
            password = json.loads(raw or b"{}").get("password", "")
        except (ValueError, AttributeError):
            password = ""
        if not isinstance(password, str) or not password:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "password_required"})

        token = self.auth.login(password)
        if not token:
            locked = self.auth.locked_for()
            if locked:
                return self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "locked", "locked_sec": locked})
            return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "wrong_password"})
        return self._send_json(HTTPStatus.OK, {"ok": True},
                               {"Set-Cookie": self._cookie(token, self.auth.ABSOLUTE_SEC)})

    def _control_post(self, path: str, raw: bytes) -> None:
        if not self._is_admin():
            return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "login_required"})
        if self.auth.kind == "pin" and self.control.hardware_real():
            return self._send_json(HTTPStatus.FORBIDDEN, {
                "error": "strong_password_required",
                "message": "실제 출력을 사용하는 동안에는 PIN 으로 제어할 수 없습니다. 10자 이상 비밀번호로 바꾸세요."})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_json"})
        if path == "/admin/api/control/command":
            status, obj = self.control.command(body)
        else:
            status, obj = self.control.save_config(body)
        return self._send_json(status, obj)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815


def make_server(host: str, port: int, db_path: Path, status_path: Path,
                admin_hash: str | None = None, config_path: Path = DEFAULT_CONFIG,
                log_path: Path = DEFAULT_LOG, admin_kind: str = "password",
                control_db: Path = DEFAULT_CONTROL_DB,
                control_status: Path = DEFAULT_CONTROL_STATUS) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {
        "data": DataSource(db_path, status_path),
        "auth": admin_mod.AdminAuth(admin_hash, admin_kind),
        "paths": {"root": ROOT, "db": Path(db_path), "config": Path(config_path), "log": Path(log_path)},
        "control": ControlAPI(control_db, control_status, config_path),
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description="스마트팜 공개(보기 전용) 웹 대시보드")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(DEFAULT_ENV)
    except ImportError:
        pass
    admin_hash = os.environ.get("ADMIN_PASSWORD_HASH") or None
    if not admin_hash:
        print("ADMIN_PASSWORD_HASH 미설정 → 관리자 로그인 비활성 "
              "(python -m smartfarm.web.admin_password 로 설정)", flush=True)

    admin_kind = os.environ.get("ADMIN_PASSWORD_KIND", "password")
    httpd = make_server(args.host, args.port, args.db, args.status,
                        admin_hash=admin_hash, admin_kind=admin_kind)
    print(f"smartfarm-web listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
