"""텔레그램 봇 명령 응답 생성 (/status, /temp, /alerts, /help).

메인 서버와 같은 데이터만 읽는다: data/status.json(현재값), data/smartfarm.db(읽기 전용),
config/config.yaml(기준값), logs/smartfarm.log(최근 오류 수). 밸브 제어 등 쓰기 동작은 없다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
STATUS_PATH = ROOT / "data" / "status.json"
DB_PATH = ROOT / "data" / "smartfarm.db"
CONFIG_PATH = ROOT / "config" / "config.yaml"
LOG_PATH = ROOT / "logs" / "smartfarm.log"

STALE_FACTOR = 3
CPU_WARN_C = 75.0
DISK_WARN_BYTES = 1 << 30          # 1 GB
SERVICES = (("smartfarm", "측정 서버"), ("smartfarm-web", "웹"), ("smartfarm-bot", "텔레그램 봇"))

TYPE_INFO = {          # 종류 → (아이콘, 이름, 단위, 소수 자리)
    "temperature": ("🌡", "온도", "°C", 1),
    "humidity": ("💧", "습도", "%", 1),
    "ph": ("🧪", "pH", "", 2),
    "ec": ("⚡", "EC", "mS/cm", 2),
}
ROLE_NAMES = {
    "nutrient_supply": "양액 공급", "drain": "배수",
    "irrigation_a": "관수 A", "irrigation_b": "관수 B",
}

HELP_TEXT = (
    "🌱 스마트팜 봇 명령어\n"
    "\n"
    "/status — 전체 상태 (센서·밸브·서비스·Pi)\n"
    "/temp — 온도센서 현재값과 24시간 최저/최고\n"
    "/alerts — 기준 이탈·장애 경고\n"
    "/help — 이 도움말"
)

COMMANDS = [   # setMyCommands 용 (텔레그램 입력창 메뉴)
    ("status", "전체 상태"),
    ("temp", "온도 현재값·24시간 최저/최고"),
    ("alerts", "기준 이탈·장애 경고"),
    ("help", "명령어 도움말"),
]


# ── 데이터 읽기 ────────────────────────────────────────────
def load_status(path: Path = STATUS_PATH) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_rules(path: Path = CONFIG_PATH) -> dict:
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    rules = cfg.get("rules")
    return rules if isinstance(rules, dict) else {}


def temp_range_24h(db_path: Path = DB_PATH, now: datetime | None = None) -> dict[str, tuple[float, float]]:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT sensor, MIN(value), MAX(value) FROM readings "
            "WHERE type = 'temperature' AND ts >= ? GROUP BY sensor", (since,)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    return {r[0]: (r[1], r[2]) for r in rows}


def recent_log_errors(path: Path = LOG_PATH, hours: float = 1, now: datetime | None = None,
                      max_bytes: int = 256_000) -> int:
    """최근 N시간 동안 로그의 ERROR/CRITICAL 줄 수 (로그 시각은 로컬 시간)."""
    now = now or datetime.now()
    since = now - timedelta(hours=hours)
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    n = 0
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or parts[2] not in ("ERROR", "CRITICAL"):
            continue
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts >= since:
            n += 1
    return n


def _system():
    from ..web.admin import _service_state, system_info   # 무거운 import 는 필요할 때만
    return system_info(ROOT), {name: _service_state(name) for name, _ in SERVICES}


# ── 포맷 도우미 ────────────────────────────────────────────
def _fmt(value, type_) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    _, _, unit, dp = TYPE_INFO.get(type_, ("", type_, "", 2))
    return f"{value:.{dp}f}{(' ' + unit) if unit else ''}"


def _bytes(n) -> str:
    if not isinstance(n, (int, float)):
        return "—"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "—"


def _dur(sec) -> str:
    if not isinstance(sec, (int, float)):
        return "—"
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d}일 {h}시간"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분" if m else f"{sec}초"


def _updated(status: dict | None, now: datetime) -> tuple[datetime | None, float | None, bool]:
    """(갱신 시각, 경과 초, 중단 여부)"""
    if not status:
        return None, None, True
    try:
        ts = datetime.fromisoformat(status["updated_at"])
    except (KeyError, TypeError, ValueError):
        return None, None, True
    age = (now - ts).total_seconds()
    interval = status.get("interval_sec") or 30
    return ts, age, age > interval * STALE_FACTOR


def _local(ts: datetime) -> str:
    return ts.astimezone().strftime("%m/%d %H:%M:%S")


def _breach(value, type_, rules) -> str | None:
    """기준 이탈이면 '최대 30.0 °C 초과' 같은 설명, 아니면 None."""
    r = rules.get(type_) if isinstance(rules, dict) else None
    if not isinstance(r, dict) or not isinstance(value, (int, float)):
        return None
    lo, hi = r.get("min"), r.get("max")
    if isinstance(hi, (int, float)) and value > hi:
        return f"최대 {_fmt(hi, type_)} 초과"
    if isinstance(lo, (int, float)) and value < lo:
        return f"최소 {_fmt(lo, type_)} 미만"
    return None


def _stale_line(status, ts, age) -> str:
    if status is None:
        return "⚠ 측정 데이터 없음 (측정 서버가 실행 중인지 확인)"
    if ts is None:
        return "⚠ 측정 데이터 형식 오류"
    return f"⚠ 측정 갱신 중단 — 마지막 갱신 {_local(ts)} ({_dur(age)} 전)"


# ── 명령 ───────────────────────────────────────────────────
def cmd_help() -> str:
    return HELP_TEXT


def cmd_temp(status=None, rules=None, ranges=None, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    status = load_status() if status is None else status
    rules = load_rules() if rules is None else rules
    ranges = temp_range_24h(now=now) if ranges is None else ranges
    ts, age, stale = _updated(status, now)
    temps = [s for s in (status or {}).get("sensors", []) if s.get("type") == "temperature"]
    head = "🌡 온도" + (f" ({_local(ts)} 기준)" if ts else "")
    lines = [head]
    if stale:
        lines.append(_stale_line(status, ts, age))
    if not temps:
        lines.append("온도센서 없음")
    for s in temps:
        v = s.get("value")
        mark = " ⚠" if _breach(v, "temperature", rules) else ""
        sim = " (모의값)" if s.get("simulated") else ""
        lines.append(f"• {s.get('name')}: {_fmt(v, 'temperature')}{mark}{sim}")
        rg = ranges.get(s.get("name"))
        if rg and rg[0] is not None:
            lines.append(f"   24시간 {_fmt(rg[0], 'temperature')} ~ {_fmt(rg[1], 'temperature')}")
    r = rules.get("temperature") if isinstance(rules, dict) else None
    if isinstance(r, dict) and (r.get("min") is not None or r.get("max") is not None):
        parts = []
        if r.get("min") is not None:
            parts.append(f"최소 {_fmt(r['min'], 'temperature')}")
        if r.get("max") is not None:
            parts.append(f"최대 {_fmt(r['max'], 'temperature')}")
        lines.append("기준: " + " · ".join(parts))
    return "\n".join(lines)


def cmd_status(status=None, rules=None, system=None, services=None, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    status = load_status() if status is None else status
    rules = load_rules() if rules is None else rules
    if system is None or services is None:
        system, services = _system()
    ts, age, stale = _updated(status, now)

    lines = ["📊 스마트팜 상태" + (f" ({_local(ts)} 기준)" if ts else "")]
    if stale:
        lines.append(_stale_line(status, ts, age))
    else:
        lines.append(f"측정: 정상 ({(status or {}).get('interval_sec', '—')}초 주기)")

    sensors = (status or {}).get("sensors", [])
    if sensors:
        lines.append("")
        lines.append("[센서]")
        for s in sensors:
            t = s.get("type")
            icon = TYPE_INFO.get(t, ("•",))[0]
            mark = " ⚠" if _breach(s.get("value"), t, rules) else ""
            sim = " (모의값)" if s.get("simulated") else ""
            lines.append(f"{icon} {s.get('name')}: {_fmt(s.get('value'), t)}{mark}{sim}")

    valves = (status or {}).get("valves", [])
    if valves:
        lines.append("")
        lines.append("[밸브]")
        for v in valves:
            role = ROLE_NAMES.get(v.get("role"), v.get("role") or "")
            lines.append(f"• {v.get('name')} ({role}): {'🟢 열림' if v.get('open') else '⚪ 닫힘'}")

    lines.append("")
    lines.append("[서비스]")
    for name, label in SERVICES:
        st = services.get(name, "unknown")
        lines.append(f"• {label}: {'✅ 실행 중' if st == 'active' else '❌ ' + st}")

    lines.append("")
    lines.append("[Pi]")
    cpu = system.get("cpu_temp_c")
    lines.append(f"CPU 온도 {cpu if cpu is not None else '—'} °C · 가동 {_dur(system.get('uptime_sec'))}")
    lines.append(f"메모리 여유 {_bytes(system.get('mem_available'))} · 디스크 여유 {_bytes(system.get('disk_free'))}")
    return "\n".join(lines)


def collect_alerts(status, rules, system, services, log_errors, now) -> list[str]:
    alerts = []
    ts, age, stale = _updated(status, now)
    if stale:
        alerts.append(_stale_line(status, ts, age))
    for s in (status or {}).get("sensors", []):
        t, v = s.get("type"), s.get("value")
        why = _breach(v, t, rules)
        if why:
            name = TYPE_INFO.get(t, ("", t))[1]
            sim = " (모의값)" if s.get("simulated") else ""
            alerts.append(f"🔴 {s.get('name')} {name} {_fmt(v, t)} — {why}{sim}")
        elif v is None and not stale:
            alerts.append(f"⚠ {s.get('name')} 값 없음 (센서 읽기 실패)")
    for name, label in SERVICES:
        st = services.get(name, "unknown")
        if st != "active":
            alerts.append(f"❌ {label} 서비스 {st}")
    cpu = system.get("cpu_temp_c")
    if isinstance(cpu, (int, float)) and cpu >= CPU_WARN_C:
        alerts.append(f"🔥 Pi CPU 온도 높음: {cpu} °C")
    free = system.get("disk_free")
    if isinstance(free, (int, float)) and free < DISK_WARN_BYTES:
        alerts.append(f"💾 디스크 여유 부족: {_bytes(free)}")
    if log_errors:
        alerts.append(f"📝 최근 1시간 로그 오류 {log_errors}건 (관리자 페이지에서 확인)")
    return alerts


def cmd_alerts(status=None, rules=None, system=None, services=None, log_errors=None, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    status = load_status() if status is None else status
    rules = load_rules() if rules is None else rules
    if system is None or services is None:
        system, services = _system()
    log_errors = recent_log_errors() if log_errors is None else log_errors
    alerts = collect_alerts(status, rules, system, services, log_errors, now)
    when = datetime.now().astimezone().strftime("%m/%d %H:%M")
    if not alerts:
        return f"✅ 현재 경고 없음 ({when})\n모든 센서가 기준 범위 안이고 서비스가 정상입니다."
    return f"🚨 경고 {len(alerts)}건 ({when})\n\n" + "\n".join(alerts)


HANDLERS = {
    "status": cmd_status,
    "temp": cmd_temp,
    "alerts": cmd_alerts,
    "help": cmd_help,
    "start": cmd_help,
}


def parse_command(text: str | None, bot_username: str | None = None) -> str | None:
    """'/temp', '/temp@MyBot 인자' → 'temp'. 다른 봇을 지목했거나 명령이 아니면 None."""
    if not text or not text.startswith("/"):
        return None
    word = text.split(None, 1)[0][1:]
    name, _, target = word.partition("@")
    if target and bot_username and target.lower() != bot_username.lower():
        return None
    return name.lower() or None


def reply_for(command: str) -> str:
    handler = HANDLERS.get(command)
    if handler is None:
        return "알 수 없는 명령입니다.\n\n" + HELP_TEXT
    return handler()
