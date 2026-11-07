"""공개(보기 전용) 웹 대시보드 서버.

구조 (RTK Base 공개 게이트웨이와 동일한 방식):
    인터넷 → Tailscale Funnel(HTTPS) → 127.0.0.1:8081 (이 서버)

- 표준 라이브러리만 사용. 루프백 주소에서만 수신한다.
- GET/HEAD 만 허용. 제어(밸브 등) 기능은 없다.
- 데이터 출처: data/status.json(메인 서버가 기록), data/smartfarm.db(읽기 전용으로 열기).
- 응답 필드는 허용 목록으로 새로 구성한다(핀·주소·로그·설정은 내보내지 않음).

실행:
    PYTHONPATH=src .venv/bin/python -m smartfarm.web.public_server --port 8081
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "data" / "smartfarm.db"
DEFAULT_STATUS = ROOT / "data" / "status.json"
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
    data: DataSource  # make_server 에서 주입

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

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")

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

        if path == "/api/v1/health":
            st = self.data.status()
            return self._json({"state": st["state"], "updated_at": st["updated_at"]})

        return self._not_found()

    do_HEAD = do_GET  # noqa: N815

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed  # noqa: N815


def make_server(host: str, port: int, db_path: Path, status_path: Path) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"data": DataSource(db_path, status_path)})
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

    httpd = make_server(args.host, args.port, args.db, args.status)
    print(f"smartfarm-web listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
