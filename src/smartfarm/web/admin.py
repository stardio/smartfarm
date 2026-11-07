"""관리자 인증(비밀번호 → 세션 쿠키)과 관리자용 상세 정보.

- 비밀번호는 평문으로 저장하지 않는다. `.env` 의 ADMIN_PASSWORD_HASH 에
  scrypt 해시만 둔다(설정: python -m smartfarm.web.admin_password).
- 세션은 서버 메모리에만 있다(웹서버 재시작 시 모두 로그아웃).
- 비밀번호 형식: 숫자 4자리 PIN 또는 10자 이상 비밀번호.
- 로그인 실패가 반복되면 전체 로그인을 잠그고, 잠길 때마다 잠금 시간을 2배로 늘린다
  (15분 → 30분 → … 최대 24시간). PIN 은 경우의 수가 1만 개뿐이라 이 단계적 잠금이
  무차별 대입을 사실상 막는 핵심 장치다. Funnel 뒤에서는 클라이언트 IP 를 신뢰할 수
  없으므로 IP 별이 아니라 전역으로 제한한다.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

# ── 비밀번호 해시 ──────────────────────────────────────────
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LEN = 10
PIN_RE = re.compile(r"[0-9]{4}")


def secret_kind(password: str) -> str | None:
    """허용 형식이면 'pin' 또는 'password', 아니면 None."""
    if PIN_RE.fullmatch(password):
        return "pin"
    if len(password) >= MIN_PASSWORD_LEN:
        return "password"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_hex, dk_hex = stored.split(":")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(dk_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), dk_hex)


# ── 세션 / 로그인 제한 ─────────────────────────────────────
class AdminAuth:
    COOKIE = "__Host-sf_admin"
    IDLE_SEC = 2 * 3600          # 2시간 동안 사용 없으면 만료
    ABSOLUTE_SEC = 12 * 3600     # 로그인 후 최대 12시간
    MAX_SESSIONS = 20
    FAIL_WINDOW_SEC = 15 * 60
    FAIL_LIMIT = 5               # 15분 안에 5번 틀리면 잠금
    LOCK_SEC = 15 * 60           # 첫 잠금 15분, 이후 잠길 때마다 2배
    LOCK_MAX_SEC = 24 * 3600     # 최대 24시간
    LEVEL_RESET_SEC = 24 * 3600  # 24시간 동안 실패가 없으면 잠금 단계 초기화
    FAIL_DELAY_SEC = 1.0         # 실패 응답 지연(무차별 대입 속도 저하)

    def __init__(self, password_hash: str | None, kind: str = "password"):
        self.password_hash = password_hash or None
        self.kind = "pin" if kind == "pin" else "password"
        self._sessions: dict[str, tuple[float, float]] = {}   # token → (created, last_seen)
        self._fails: deque[float] = deque()
        self._locked_until = 0.0
        self._lock_level = 0
        self._last_fail = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)

    def locked_for(self) -> int:
        with self._lock:
            return max(0, int(self._locked_until - time.monotonic()))

    def login(self, password: str) -> str | None:
        """성공 시 세션 토큰, 실패 시 None."""
        if self.locked_for() or not self.configured:
            return None
        if verify_password(password, self.password_hash):
            token = secrets.token_urlsafe(32)
            now = time.monotonic()
            with self._lock:
                self._prune(now)
                if len(self._sessions) >= self.MAX_SESSIONS:   # 가장 오래된 세션 제거
                    oldest = min(self._sessions, key=lambda t: self._sessions[t][1])
                    del self._sessions[oldest]
                self._sessions[token] = (now, now)
                self._fails.clear()
                self._lock_level = 0
            return token
        now = time.monotonic()
        with self._lock:
            if self._last_fail and now - self._last_fail > self.LEVEL_RESET_SEC:
                self._lock_level = 0
            self._last_fail = now
            self._fails.append(now)
            while self._fails and now - self._fails[0] > self.FAIL_WINDOW_SEC:
                self._fails.popleft()
            if len(self._fails) >= self.FAIL_LIMIT:
                dur = min(self.LOCK_SEC * (2 ** self._lock_level), self.LOCK_MAX_SEC)
                self._locked_until = now + dur
                self._lock_level += 1
                self._fails.clear()
        time.sleep(self.FAIL_DELAY_SEC)
        return None

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            s = self._sessions.get(token)
            if not s:
                return False
            created, last = s
            if now - last > self.IDLE_SEC or now - created > self.ABSOLUTE_SEC:
                del self._sessions[token]
                return False
            self._sessions[token] = (created, now)
            return True

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def _prune(self, now: float) -> None:
        dead = [t for t, (c, l) in self._sessions.items()
                if now - l > self.IDLE_SEC or now - c > self.ABSOLUTE_SEC]
        for t in dead:
            del self._sessions[t]


# ── 관리자용 상세 정보 ─────────────────────────────────────
_REDACT = [
    (re.compile(r"(/bot)[^/\s]+"), r"\1<redacted>"),                     # 텔레그램 토큰 URL
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "<redacted>"),          # 토큰 형태
    (re.compile(r"(?i)((?:token|password|secret|chat_id)\S*\s*[=:]\s*)\S+"), r"\1<redacted>"),
]


def redact(line: str) -> str:
    for pat, rep in _REDACT:
        line = pat.sub(rep, line)
    return line


def tail_lines(path: Path, n: int = 80, max_bytes: int = 64_000) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()[-n:]
    return [redact(l)[:500] for l in lines]


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _service_state(name: str) -> str:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", name],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def system_info(root: Path) -> dict:
    info: dict = {"hostname": socket.gethostname()}
    up = _read("/proc/uptime")
    info["uptime_sec"] = int(float(up.split()[0])) if up else None
    load = _read("/proc/loadavg")
    info["load"] = [float(x) for x in load.split()[:3]] if load else None
    t = _read("/sys/class/thermal/thermal_zone0/temp")
    info["cpu_temp_c"] = round(int(t) / 1000, 1) if t and t.isdigit() else None
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) * 1024
    info["mem_total"] = mem.get("MemTotal")
    info["mem_available"] = mem.get("MemAvailable")
    try:
        du = shutil.disk_usage(root)
        info["disk_total"], info["disk_free"] = du.total, du.free
    except OSError:
        info["disk_total"] = info["disk_free"] = None
    return info


def db_info(db_path: Path) -> dict:
    out: dict = {"size_bytes": None, "rows": None, "first_ts": None, "last_ts": None}
    try:
        out["size_bytes"] = db_path.stat().st_size
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            out["rows"], out["first_ts"], out["last_ts"] = conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM readings").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        pass
    return out


def config_info(config_path: Path) -> dict:
    """장비 구성 상세(관리자 전용). yaml 은 여기서만 직접 읽는다."""
    try:
        import yaml  # venv 의존성
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {"error": "설정 파일을 읽을 수 없습니다."}
    sensors = [
        {k: (str(s.get(k)) if s.get(k) is not None else None)
         for k in ("name", "type", "interface", "address", "channel")}
        for s in cfg.get("sensors") or [] if isinstance(s, dict)
    ]
    valves = [
        {"name": v.get("name"), "pin": v.get("pin"), "active_high": bool(v.get("active_high", False)),
         "role": v.get("role")}
        for v in cfg.get("valves") or [] if isinstance(v, dict)
    ]
    return {
        "sampling_interval_sec": cfg.get("sampling_interval_sec"),
        "sensors": sensors,
        "valves": valves,
        "rules": cfg.get("rules") or {},
        "alerts": cfg.get("alerts") or {},
    }


def overview(root: Path, db_path: Path, config_path: Path, log_path: Path) -> dict:
    return {
        "services": {n: _service_state(n) for n in ("smartfarm", "smartfarm-web")},
        "system": system_info(root),
        "db": db_info(db_path),
        "config": config_info(config_path),
        "logs": tail_lines(log_path),
    }
