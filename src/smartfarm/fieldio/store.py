"""I/O 구성 저장 (control.db 안의 io_* 테이블) 과 검증.

  io_buses   통신 버스: tcp(게이트웨이·이더넷 보드) | rtu(USB-RS485) | sim(시뮬레이터)
  io_devices 장치: 버스 + 국번 + 프로파일 + 옵션
  io_map     출력 할당: 채널(예: irrigation.3) → 장치 + 릴레이 번호(0부터)
  io_sensors 센서 할당: 센서 이름 → 장치 + 측정점(종류·기능코드·번지·배율·부호)
  meta.io_version  구성이 바뀔 때마다 +1 → 측정 서버가 다시 읽는다
"""
from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import time
from pathlib import Path

from ..control.store import CONTROL_DB
from ..control.systems import ORDER, SYSTEMS
from .profiles import PROFILES, SENSOR_TYPES, relay_options

SCHEMA = """
CREATE TABLE IF NOT EXISTS io_buses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
  params TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS io_devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT, bus_id INTEGER NOT NULL, unit INTEGER NOT NULL,
  name TEXT NOT NULL, profile TEXT NOT NULL, options TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS io_map (
  channel TEXT PRIMARY KEY, device_id INTEGER NOT NULL, idx INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_io_map_relay ON io_map(device_id, idx);
CREATE TABLE IF NOT EXISTS io_sensors (
  name TEXT PRIMARY KEY, device_id INTEGER NOT NULL, point TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT);
"""
BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
_NAME = re.compile(r"[A-Za-z0-9_\-가-힣 ]{1,24}")
_SENSOR_NAME = re.compile(r"[a-z][a-z0-9_]{0,23}")
_SERIAL = re.compile(r"/dev/(tty[A-Za-z0-9]+|serial/by-(id|path)/[A-Za-z0-9_.:\-]+)")


class IOConfigError(ValueError):
    pass


def channel_keys() -> list[tuple[str, str]]:
    """(채널 키, 표시 이름) — 할당 가능한 모든 출력."""
    out = [("irrigation.pump", "관수 주 펌프")]
    for sid in ORDER:
        spec = SYSTEMS[sid]
        for z in range(1, spec["zones"] + 1):
            label = spec["channels"][z - 1] if spec["kind"] == "nutrient" else f"{spec['name']} {z}구역"
            out.append((f"{sid}.{z}", label))
    return out


CHANNEL_KEYS = {k for k, _ in channel_keys()}


def _int(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise IOConfigError(f"{what}: {lo}~{hi} 정수로 입력하세요")
    return v


def _name(v, what="이름"):
    v = str(v or "").strip()
    if not _NAME.fullmatch(v):
        raise IOConfigError(f"{what}: 한글·영문·숫자 1~24자")
    return v


def validate_bus(d: dict) -> dict:
    if not isinstance(d, dict):
        raise IOConfigError("형식 오류")
    kind = d.get("kind")
    out = {"name": _name(d.get("name")), "kind": kind, "enabled": bool(d.get("enabled", True))}
    timeout = d.get("timeout", 0.5)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 3:
        raise IOConfigError("응답 대기 시간: 0.1~3초")
    p: dict = {"timeout": float(timeout)}
    if kind == "tcp":
        host = str(d.get("host") or "").strip()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9.\-]{1,63}", host):
                raise IOConfigError("주소: IP(예: 192.168.10.20) 또는 호스트 이름") from None
        p.update(host=host, port=_int(d.get("port", 502), 1, 65535, "포트"))
    elif kind == "rtu":
        port = str(d.get("port") or "").strip()
        if not _SERIAL.fullmatch(port):
            raise IOConfigError("시리얼 포트: /dev/ttyUSB0 또는 /dev/serial/by-id/… 형식")
        baud = d.get("baudrate", 9600)
        if baud not in BAUDS:
            raise IOConfigError("통신 속도를 목록에서 선택하세요")
        parity = d.get("parity", "N")
        if parity not in ("N", "E", "O"):
            raise IOConfigError("패리티: N / E / O")
        p.update(port=port, baudrate=baud, parity=parity, stopbits=_int(d.get("stopbits", 1), 1, 2, "정지 비트"))
    elif kind == "sim":
        pass
    else:
        raise IOConfigError("버스 종류: tcp / rtu / sim")
    out["params"] = p
    return out


def validate_device(d: dict) -> dict:
    if not isinstance(d, dict):
        raise IOConfigError("형식 오류")
    pid = d.get("profile")
    if pid not in PROFILES:
        raise IOConfigError("프로파일을 목록에서 선택하세요")
    out = {"name": _name(d.get("name")), "bus_id": _int(d.get("bus_id"), 1, 10**9, "버스"),
           "unit": _int(d.get("unit"), 1, 247, "국번"), "profile": pid, "enabled": bool(d.get("enabled", True))}
    if PROFILES[pid]["kind"] == "relay":
        o = relay_options(pid, d.get("options"))
        _int(o["channels"], 1, 64, "릴레이 수")
        _int(o["base"], 0, 65535, "시작 번지")
        if o["write"] not in ("coil", "register"):
            raise IOConfigError("쓰기 방식: coil / register")
        if o["readback"] not in ("coil", "holding", "none"):
            raise IOConfigError("상태 읽기: coil / holding / none")
        if o["write"] == "register":
            _int(o["on_value"], 0, 65535, "켜기 값")
            _int(o["off_value"], 0, 65535, "끄기 값")
        out["options"] = o
    else:
        out["options"] = {}
    return out


def validate_point(p: dict) -> dict:
    if not isinstance(p, dict) or p.get("type") not in SENSOR_TYPES:
        raise IOConfigError("측정 종류: " + " / ".join(SENSOR_TYPES))
    if p.get("fn") not in (3, 4):
        raise IOConfigError("기능 코드: 3(홀딩) / 4(입력)")
    scale = p.get("scale", 1)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 < abs(scale) <= 1000:
        raise IOConfigError("배율: 예 0.1")
    return {"type": p["type"], "fn": p["fn"], "addr": _int(p.get("addr"), 0, 65535, "번지"),
            "scale": float(scale), "signed": bool(p.get("signed", False))}


class IOStore:
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

    def _connect(self):
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

    def _changed(self, c, action, detail, source="web"):
        c.execute("INSERT INTO meta (key, value) VALUES ('io_version', '1') "
                  "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
        c.execute("INSERT INTO events (ts, source, action, detail) VALUES (?,?,?,?)",
                  (time.time(), source, action, detail[:500]))

    def version(self) -> int:
        row = self._run(lambda c: c.execute("SELECT value FROM meta WHERE key='io_version'").fetchone())
        return int(row[0]) if row else 0

    # ── 조회 ───────────────────────────────────────────────
    def load(self) -> dict:
        def fn(c):
            buses = [{"id": i, "name": n, "kind": k, "params": json.loads(p), "enabled": bool(e)}
                     for i, n, k, p, e in c.execute("SELECT id, name, kind, params, enabled FROM io_buses ORDER BY id")]
            devices = [{"id": i, "bus_id": b, "unit": u, "name": n, "profile": pr, "options": json.loads(o),
                        "enabled": bool(e)}
                       for i, b, u, n, pr, o, e in c.execute(
                           "SELECT id, bus_id, unit, name, profile, options, enabled FROM io_devices ORDER BY bus_id, unit")]
            mapping = {ch: {"device_id": d, "idx": i} for ch, d, i in c.execute("SELECT channel, device_id, idx FROM io_map")}
            sensors = [{"name": n, "device_id": d, "point": json.loads(p), "enabled": bool(e)}
                       for n, d, p, e in c.execute("SELECT name, device_id, point, enabled FROM io_sensors ORDER BY name")]
            return {"buses": buses, "devices": devices, "map": mapping, "sensors": sensors}
        return self._run(fn)

    # ── 버스 ───────────────────────────────────────────────
    def save_bus(self, data: dict, bus_id: int | None = None) -> int:
        b = validate_bus(data)

        def fn(c):
            if bus_id:
                if not c.execute("SELECT 1 FROM io_buses WHERE id=?", (bus_id,)).fetchone():
                    raise IOConfigError("없는 버스")
                c.execute("UPDATE io_buses SET name=?, kind=?, params=?, enabled=? WHERE id=?",
                          (b["name"], b["kind"], json.dumps(b["params"]), int(b["enabled"]), bus_id))
                rid = bus_id
            else:
                rid = c.execute("INSERT INTO io_buses (name, kind, params, enabled) VALUES (?,?,?,?)",
                                (b["name"], b["kind"], json.dumps(b["params"]), int(b["enabled"]))).lastrowid
            self._changed(c, "io_bus", f"{rid} {b['name']} {b['kind']}")
            return rid
        return self._run(fn)

    def delete_bus(self, bus_id: int) -> None:
        def fn(c):
            ids = [r[0] for r in c.execute("SELECT id FROM io_devices WHERE bus_id=?", (bus_id,))]
            for d in ids:
                self._delete_device(c, d)
            c.execute("DELETE FROM io_buses WHERE id=?", (bus_id,))
            self._changed(c, "io_bus_delete", str(bus_id))
        self._run(fn)

    # ── 장치 ───────────────────────────────────────────────
    def save_device(self, data: dict, device_id: int | None = None) -> int:
        d = validate_device(data)

        def fn(c):
            if not c.execute("SELECT 1 FROM io_buses WHERE id=?", (d["bus_id"],)).fetchone():
                raise IOConfigError("없는 버스")
            dup = c.execute("SELECT id FROM io_devices WHERE bus_id=? AND unit=? AND id IS NOT ?",
                            (d["bus_id"], d["unit"], device_id)).fetchone()
            if dup:
                raise IOConfigError(f"같은 버스에 국번 {d['unit']} 장치가 이미 있습니다")
            if device_id:
                old = c.execute("SELECT profile FROM io_devices WHERE id=?", (device_id,)).fetchone()
                if not old:
                    raise IOConfigError("없는 장치")
                c.execute("UPDATE io_devices SET bus_id=?, unit=?, name=?, profile=?, options=?, enabled=? WHERE id=?",
                          (d["bus_id"], d["unit"], d["name"], d["profile"], json.dumps(d["options"]),
                           int(d["enabled"]), device_id))
                kind = PROFILES[d["profile"]]["kind"]
                if kind != "relay":
                    c.execute("DELETE FROM io_map WHERE device_id=?", (device_id,))
                else:
                    c.execute("DELETE FROM io_map WHERE device_id=? AND idx>=?", (device_id, d["options"]["channels"]))
                if kind != "sensor":
                    c.execute("DELETE FROM io_sensors WHERE device_id=?", (device_id,))
                rid = device_id
            else:
                rid = c.execute("INSERT INTO io_devices (bus_id, unit, name, profile, options, enabled) VALUES (?,?,?,?,?,?)",
                                (d["bus_id"], d["unit"], d["name"], d["profile"], json.dumps(d["options"]),
                                 int(d["enabled"]))).lastrowid
            self._changed(c, "io_device", f"{rid} {d['name']} unit={d['unit']} {d['profile']}")
            return rid
        return self._run(fn)

    def _delete_device(self, c, device_id):
        c.execute("DELETE FROM io_map WHERE device_id=?", (device_id,))
        c.execute("DELETE FROM io_sensors WHERE device_id=?", (device_id,))
        c.execute("DELETE FROM io_devices WHERE id=?", (device_id,))

    def delete_device(self, device_id: int) -> None:
        def fn(c):
            self._delete_device(c, device_id)
            self._changed(c, "io_device_delete", str(device_id))
        self._run(fn)

    # ── 출력 할당 ──────────────────────────────────────────
    def set_map(self, device_id: int, idx: int, channel: str | None) -> None:
        def fn(c):
            row = c.execute("SELECT profile, options FROM io_devices WHERE id=?", (device_id,)).fetchone()
            if not row or PROFILES.get(row[0], {}).get("kind") != "relay":
                raise IOConfigError("릴레이 장치가 아닙니다")
            n = json.loads(row[1]).get("channels", 0)
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < n:
                raise IOConfigError("릴레이 번호 오류")
            c.execute("DELETE FROM io_map WHERE device_id=? AND idx=?", (device_id, idx))
            if channel:
                if channel not in CHANNEL_KEYS:
                    raise IOConfigError("할당할 출력을 목록에서 선택하세요")
                c.execute("DELETE FROM io_map WHERE channel=?", (channel,))      # 다른 릴레이에 있던 할당은 옮김
                c.execute("INSERT INTO io_map (channel, device_id, idx) VALUES (?,?,?)", (channel, device_id, idx))
            self._changed(c, "io_map", f"device {device_id} #{idx + 1} → {channel or '(해제)'}")
        self._run(fn)

    # ── 센서 할당 ──────────────────────────────────────────
    def save_sensor(self, name: str, device_id: int, point: dict, enabled: bool = True,
                    reserved: set[str] | None = None) -> None:
        name = str(name or "").strip()
        if not _SENSOR_NAME.fullmatch(name):
            raise IOConfigError("센서 이름: 영문 소문자로 시작, 소문자·숫자·_ (예: temp_z1)")
        if reserved and name in reserved:
            raise IOConfigError(f"{name} 은 config.yaml 센서와 이름이 겹칩니다")
        pt = validate_point(point)

        def fn(c):
            row = c.execute("SELECT profile FROM io_devices WHERE id=?", (device_id,)).fetchone()
            if not row or PROFILES.get(row[0], {}).get("kind") != "sensor":
                raise IOConfigError("센서 장치가 아닙니다")
            c.execute("INSERT INTO io_sensors (name, device_id, point, enabled) VALUES (?,?,?,?) "
                      "ON CONFLICT(name) DO UPDATE SET device_id=excluded.device_id, point=excluded.point, "
                      "enabled=excluded.enabled", (name, device_id, json.dumps(pt), int(enabled)))
            self._changed(c, "io_sensor", f"{name} → device {device_id} {pt}")
        self._run(fn)

    def delete_sensor(self, name: str) -> None:
        def fn(c):
            c.execute("DELETE FROM io_sensors WHERE name=?", (name,))
            self._changed(c, "io_sensor_delete", name)
        self._run(fn)

    def has_real_devices(self) -> bool:
        """시뮬레이터가 아닌 버스에 사용 중인 장치가 있으면 True (= 실제 출력 가능)."""
        row = self._run(lambda c: c.execute(
            "SELECT 1 FROM io_devices d JOIN io_buses b ON b.id=d.bus_id "
            "WHERE d.enabled=1 AND b.enabled=1 AND b.kind!='sim' LIMIT 1").fetchone())
        return bool(row)
