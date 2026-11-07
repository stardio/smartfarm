"""필드 I/O 관리자: Modbus 버스·장치를 열고, 출력 쓰기·상태 확인·센서 읽기를 담당.

측정 서버(main.py) 안에서 스레드로 돈다. 제어기는 MappedOutput 을 통해 채널(예: irrigation.3)
단위로 켜고 끄며, 어느 보드·몇 번 릴레이인지는 I/O 할당(io_map)으로 정해진다.

- 할당되지 않은 채널은 시뮬레이션(실제 출력 없음).
- 릴레이 상태를 주기적으로 다시 읽어, 명령과 다르면(보드 재부팅 등) 다시 쓴다.
- 연속 3회 통신 실패 → 장치 "끊김" + 텔레그램 알림. 끊긴 장치에는 쓰지 않고(제어 지연 방지)
  5초마다 재접속을 시도한다. 다시 연결되면 명령 상태를 다시 맞춘다.
- 장치가 처음 연결될 때 그 보드의 릴레이를 모두 끈 뒤 명령 상태를 맞춘다.
- 시운전 명령(릴레이 시험·장치 검색·레지스터 읽기)은 별도 작업 스레드에서 처리한다.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

from ..core.logging import get_logger
from ..sensors.base import Sensor
from .modbus import ModbusError, ModbusTimeout, RtuTransport, SimTransport, TcpTransport
from .profiles import PROFILES, decode, relay_options
from .sim import SimDevice
from .store import IOStore

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[3]
IO_STATUS = ROOT / "data" / "io.json"
FAIL_LIMIT = 3
RELAY_POLL_SEC = 2.0
SENSOR_POLL_SEC = 5.0
OFFLINE_RETRY_SEC = 5.0
SENSOR_MAX_AGE = 20.0
STATUS_EVERY_SEC = 2.0


class Device:
    def __init__(self, cfg: dict, bus):
        self.id, self.name, self.unit = cfg["id"], cfg["name"], cfg["unit"]
        self.profile_id = cfg["profile"]
        self.profile = PROFILES[cfg["profile"]]
        self.kind = self.profile["kind"]
        self.enabled = cfg["enabled"]
        self.opts = relay_options(cfg["profile"], cfg["options"]) if self.kind == "relay" else {}
        self.bus = bus
        self.online = False
        self.ever_online = False
        self.fails = 0
        self.error = ""
        self.last_ok = 0.0
        self.next_poll = 0.0
        self.states: list[bool] | None = None     # 마지막으로 읽은 릴레이 상태
        self.commanded: dict[int, bool] = {}      # 할당된 릴레이의 명령 상태
        self.pulse_until: dict[int, float] = {}
        self.values: dict[str, tuple[float, float]] = {}   # 센서명 → (값, 시각)
        self.raw_points: dict[str, float] = {}             # 프로파일 측정점 → 값 (화면 표시용)

    @property
    def channels(self) -> int:
        return int(self.opts.get("channels", 0))

    def label(self) -> str:
        return f"{self.name}(국번 {self.unit})"


class Bus:
    def __init__(self, cfg: dict, sims: dict):
        self.id, self.name, self.kind, self.enabled = cfg["id"], cfg["name"], cfg["kind"], cfg["enabled"]
        p = cfg["params"]
        if self.kind == "tcp":
            self.t = TcpTransport(p["host"], p["port"], p["timeout"])
            self.where = f"{p['host']}:{p['port']}"
        elif self.kind == "rtu":
            self.t = RtuTransport(p["port"], p["baudrate"], p["parity"], p["stopbits"], p["timeout"])
            self.where = f"{p['port']} {p['baudrate']}bps {p['parity']}"
        else:
            if self.id not in sims:
                # 실제 현장처럼 미리 연결된 가상 장비: 국번 1·2 = 16채널 릴레이 보드, 3·4 = 온습도 센서
                sims[self.id] = {1: SimDevice("relay", 16), 2: SimDevice("relay", 16),
                                 3: SimDevice("sensor"), 4: SimDevice("sensor")}
            self.t = SimTransport(sims[self.id], p["timeout"])
            self.where = "시뮬레이터"


class IOManager:
    def __init__(self, store: IOStore | None = None, notify=None, status_path: Path = IO_STATUS,
                 clock=time.time):
        self.store = store or IOStore()
        self.notify = notify or (lambda text: None)
        self.status_path = Path(status_path)
        self.clock = clock
        self.lock = threading.RLock()
        self.buses: dict[int, Bus] = {}
        self.devices: dict[int, Device] = {}
        self.map: dict[str, tuple[int, int]] = {}            # 채널 → (장치 id, 릴레이 번호)
        self.sensor_cfg: dict[str, dict] = {}                # 센서명 → {device_id, point, enabled}
        self.sims: dict[int, dict[int, SimDevice]] = {}      # 시뮬레이터 버스별 가짜 장비
        self.version = -1
        self.sensor_version = 0
        self.jobs: queue.Queue = queue.Queue()
        self.command_store = None                            # ControlStore (명령 결과 기록)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_status = 0.0
        self.reload(force=True)

    # ── 구성 ───────────────────────────────────────────────
    def reload(self, force=False) -> bool:
        v = self.store.version()
        if v == self.version and not force:
            return False
        cfg = self.store.load()
        with self.lock:
            old_devices = self.devices
            new_buses = {}
            for b in cfg["buses"]:
                old = self.buses.get(b["id"])
                bus = Bus(b, self.sims)
                if old and old.kind == bus.kind and old.where == bus.where and old.t.timeout == bus.t.timeout:
                    bus.t = old.t                                    # 같은 연결은 유지
                elif old:
                    old.t.close()
                new_buses[b["id"]] = bus
            for bid, old in self.buses.items():
                if bid not in new_buses:
                    old.t.close()
            new_devices = {}
            for d in cfg["devices"]:
                bus = new_buses.get(d["bus_id"])
                if not bus:
                    continue
                dev = Device(d, bus)
                prev = old_devices.get(d["id"])
                if prev and prev.unit == dev.unit and prev.bus.id == bus.id and prev.profile_id == dev.profile_id:
                    for k in ("online", "ever_online", "fails", "error", "last_ok", "states",
                              "commanded", "pulse_until", "values", "raw_points"):
                        setattr(dev, k, getattr(prev, k))
                if bus.kind == "sim":
                    sims = self.sims.setdefault(bus.id, {})
                    if dev.unit not in sims or sims[dev.unit].kind != dev.kind:
                        sims[dev.unit] = SimDevice(dev.kind, max(dev.channels, 8))
                new_devices[d["id"]] = dev
            # 할당이 풀린 릴레이는 끈다
            new_map = {ch: (m["device_id"], m["idx"]) for ch, m in cfg["map"].items() if m["device_id"] in new_devices}
            for ch, (did, idx) in self.map.items():
                if new_map.get(ch) != (did, idx) and did in new_devices:
                    dev = new_devices[did]
                    if dev.commanded.pop(idx, None) and idx not in {i for (d2, i) in new_map.values() if d2 == did}:
                        self._write(dev, idx, False, quiet=True)
            self.buses, self.devices, self.map = new_buses, new_devices, new_map
            self.sensor_cfg = {s["name"]: s for s in cfg["sensors"] if s["device_id"] in new_devices}
            self.sensor_version += 1
            self.version = v
        log.info("I/O 구성 적용: 버스 %d, 장치 %d, 출력 할당 %d, 센서 %d",
                 len(self.buses), len(self.devices), len(self.map), len(self.sensor_cfg))
        return True

    # ── 출력 ───────────────────────────────────────────────
    def _write(self, dev: Device, idx: int, on: bool, quiet=False) -> None:
        o = dev.opts
        t = dev.bus.t
        try:
            if o["write"] == "coil":
                t.write_coil(dev.unit, o["base"] + idx, on)
            else:
                t.write_register(dev.unit, o["base"] + idx, o["on_value"] if on else o["off_value"])
            self._ok(dev)
            if dev.states is not None and idx < len(dev.states):
                dev.states[idx] = on
        except (ModbusTimeout, ModbusError) as e:
            self._fail(dev, e)
            if not quiet:
                raise RuntimeError(f"{dev.label()} #{idx + 1} 쓰기 실패: {e}") from None

    def set_channel(self, key: str, on: bool) -> bool:
        """채널 출력. 할당이 없으면 False(시뮬레이션). 실패하면 RuntimeError."""
        with self.lock:
            m = self.map.get(key)
            if not m:
                return False
            dev = self.devices.get(m[0])
            if not dev or not dev.enabled or not dev.bus.enabled:
                raise RuntimeError(f"{key}: 할당된 장치가 사용 안 함 상태")
            dev.commanded[m[1]] = bool(on)
            if not dev.online:
                # 연결되면 _sync 가 명령 상태로 맞춘다. 여기서 기다리면 제어 주기가 밀린다.
                raise RuntimeError(f"{dev.label()} {'통신 끊김' if dev.ever_online else '연결 대기'}")
        self._write(dev, m[1], on)
        return True

    def channel_info(self, key: str) -> dict | None:
        m = self.map.get(key)
        if not m:
            return None
        dev = self.devices.get(m[0])
        if not dev:
            return None
        return {"label": f"{dev.name} #{m[1] + 1}", "online": dev.online, "sim": dev.bus.kind == "sim"}

    def has_real_outputs(self) -> bool:
        return any(self.devices.get(d) and self.devices[d].bus.kind != "sim" for d, _ in self.map.values())

    # ── 상태 확인 ──────────────────────────────────────────
    def _ok(self, dev: Device):
        first = not dev.online
        dev.online, dev.fails, dev.error, dev.last_ok = True, 0, "", self.clock()
        if first:
            was = dev.ever_online
            dev.ever_online = True
            log.info("장치 연결: %s", dev.label())
            if was:
                self._notify(f"✅ [통신 복구] {dev.label()} — {dev.bus.where}")
            if dev.kind == "relay":
                self._sync(dev, initial=not was)

    def _fail(self, dev: Device, e):
        dev.fails += 1
        dev.error = str(e)[:120]
        if dev.online and dev.fails >= FAIL_LIMIT:
            dev.online = False
            log.error("장치 통신 끊김: %s — %s", dev.label(), dev.error)
            self._notify(f"⚠ [통신 끊김] {dev.label()} — {dev.bus.where}\n{dev.error}\n"
                         f"이 장치에 할당된 출력은 제어되지 않습니다.")
        elif not dev.ever_online and dev.fails == FAIL_LIMIT:
            log.warning("장치 응답 없음: %s — %s", dev.label(), dev.error)

    def _notify(self, text):
        try:
            self.notify(text)
        except Exception:  # noqa: BLE001
            log.exception("I/O 알림 실패")

    def _sync(self, dev: Device, initial: bool):
        """명령 상태로 릴레이를 맞춘다. 처음 연결이면 할당 안 된 릴레이도 끈다."""
        targets = {i: dev.commanded.get(i, False) for i in range(dev.channels)} if initial else dict(dev.commanded)
        o = dev.opts
        try:
            if initial and o["write"] == "coil":
                dev.bus.t.write_coils(dev.unit, o["base"], [targets[i] for i in range(dev.channels)])
            else:
                for i, on in targets.items():
                    self._write(dev, i, on)
        except (ModbusTimeout, ModbusError, RuntimeError) as e:
            log.warning("릴레이 상태 맞춤 실패: %s — %s", dev.label(), e)

    def _read_relays(self, dev: Device):
        o = dev.opts
        if o["readback"] == "coil":
            return dev.bus.t.read_bits(dev.unit, 1, o["base"], dev.channels)
        if o["readback"] == "holding":
            return [bool(v) for v in dev.bus.t.read_registers(dev.unit, 3, o["base"], dev.channels)]
        return None

    def _poll_device(self, dev: Device, now: float):
        if dev.kind == "relay":
            for idx, until in list(dev.pulse_until.items()):
                if now >= until:
                    dev.pulse_until.pop(idx, None)
                    self._write(dev, idx, dev.commanded.get(idx, False), quiet=True)
            states = self._read_relays(dev)
            if states is None and not dev.online:
                # 상태 읽기가 없는 보드: 1번 릴레이에 현재 명령 상태를 써서 응답 확인
                o = dev.opts
                want = dev.commanded.get(0, False)
                if o["write"] == "coil":
                    dev.bus.t.write_coil(dev.unit, o["base"], want)
                else:
                    dev.bus.t.write_register(dev.unit, o["base"], o["on_value"] if want else o["off_value"])
            self._ok(dev)
            if states is not None:
                dev.states = states
                for idx, want in dev.commanded.items():
                    if idx in dev.pulse_until or idx >= len(states):
                        continue
                    if states[idx] != want:
                        log.warning("릴레이 상태 불일치 → 다시 씀: %s #%d (명령 %s, 실제 %s)",
                                    dev.label(), idx + 1, want, states[idx])
                        self._write(dev, idx, want, quiet=True)
            else:
                dev.states = [dev.commanded.get(i, False) for i in range(dev.channels)]
        else:
            vals = {}
            pts = dict(dev.profile.get("points") or {})
            for name, s in self.sensor_cfg.items():
                if s["device_id"] == dev.id and s["enabled"]:
                    pts["sensor:" + name] = s["point"]
            for pname, p in pts.items():
                raw = dev.bus.t.read_registers(dev.unit, p["fn"], p["addr"], 1)[0]
                vals[pname] = decode(raw, p["scale"], p["signed"])
            self._ok(dev)
            dev.raw_points = {k: v for k, v in vals.items() if not k.startswith("sensor:")}
            for k, v in vals.items():
                if k.startswith("sensor:"):
                    dev.values[k[7:]] = (v, now)

    def poll_once(self):
        now = self.clock()
        with self.lock:
            devices = [d for d in self.devices.values() if d.enabled and d.bus.enabled]
        for dev in devices:
            if now < dev.next_poll:
                continue
            interval = RELAY_POLL_SEC if dev.kind == "relay" else SENSOR_POLL_SEC
            try:
                self._poll_device(dev, now)
            except (ModbusTimeout, ModbusError) as e:
                self._fail(dev, e)
            dev.next_poll = now + (interval if dev.online else OFFLINE_RETRY_SEC)
        self._write_status(now)

    # ── 센서 ───────────────────────────────────────────────
    def sensors(self) -> list[Sensor]:
        with self.lock:
            out = []
            for name, s in self.sensor_cfg.items():
                if not s["enabled"]:
                    continue
                dev = self.devices[s["device_id"]]
                out.append(ModbusSensor(name, s["point"]["type"], self, dev.bus.kind == "sim"))
            return out

    def sensor_value(self, name: str):
        with self.lock:
            s = self.sensor_cfg.get(name)
            if not s:
                return None
            dev = self.devices.get(s["device_id"])
            v = dev.values.get(name) if dev else None
        if v is None or self.clock() - v[1] > SENSOR_MAX_AGE:
            return None
        return v[0]

    # ── 시운전 명령 (웹 → 제어기 → 여기) ───────────────────
    def submit(self, cid: int, cmd: dict) -> None:
        self.jobs.put((cid, cmd))

    def run_job(self, cmd: dict) -> dict:
        a = cmd.get("action")
        if a == "io_pulse":
            did, idx, sec = cmd.get("device_id"), cmd.get("idx"), cmd.get("seconds", 3)
            dev = self.devices.get(did)
            if not dev or dev.kind != "relay":
                raise ValueError("릴레이 장치가 아닙니다")
            if not isinstance(idx, int) or not 0 <= idx < dev.channels:
                raise ValueError("릴레이 번호 오류")
            if isinstance(sec, bool) or not isinstance(sec, (int, float)) or not 1 <= sec <= 10:
                raise ValueError("시험 시간은 1~10초")
            try:
                self._write(dev, idx, True)
            except RuntimeError as e:
                raise ValueError(str(e)) from None
            dev.pulse_until[idx] = self.clock() + sec
            dev.next_poll = 0
            return {"message": f"{dev.name} #{idx + 1} 릴레이 {sec:g}초 켬"}
        if a == "io_scan":
            bus = self.buses.get(cmd.get("bus_id"))
            if not bus:
                raise ValueError("없는 버스")
            lo, hi = cmd.get("start", 1), cmd.get("end", 16)
            if not (isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= hi <= 247 and hi - lo < 32):
                raise ValueError("검색 범위: 국번 1~247 중 최대 32개")
            found = []
            for unit in range(lo, hi + 1):
                if self._stop.is_set():
                    break
                for fn in (3, 1, 4):          # 장비마다 지원 기능이 달라 차례로 시도
                    try:
                        if fn == 1:
                            bus.t.read_bits(unit, 1, 0, 1)
                        else:
                            bus.t.read_registers(unit, fn, 0, 1)
                        found.append({"unit": unit, "fn": fn})
                        break
                    except ModbusError:
                        found.append({"unit": unit, "fn": fn, "exception": True})   # 응답은 함(예외)
                        break
                    except ModbusTimeout:
                        continue
            return {"message": f"국번 {lo}~{hi} 검색: {len(found)}개 응답", "found": found}
        if a == "io_read":
            dev = self.devices.get(cmd.get("device_id"))
            fn, addr, count = cmd.get("fn"), cmd.get("addr"), cmd.get("count", 1)
            if not dev:
                raise ValueError("없는 장치")
            if fn not in (1, 2, 3, 4) or not isinstance(addr, int) or not 0 <= addr <= 65535 \
                    or not isinstance(count, int) or not 1 <= count <= 16:
                raise ValueError("읽기 설정 오류 (기능 1~4, 번지 0~65535, 개수 1~16)")
            try:
                if fn in (1, 2):
                    vals = [int(b) for b in dev.bus.t.read_bits(dev.unit, fn, addr, count)]
                else:
                    vals = dev.bus.t.read_registers(dev.unit, fn, addr, count)
            except (ModbusTimeout, ModbusError) as e:
                raise ValueError(f"읽기 실패: {e}") from None
            return {"message": f"FC{fn:02d} {addr}번지부터 {count}개", "values": vals}
        raise ValueError("알 수 없는 I/O 명령")

    def _job_loop(self):
        while not self._stop.is_set():
            try:
                cid, cmd = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                res = self.run_job(cmd)
                status = "done"
            except ValueError as e:
                res, status = {"message": str(e)}, "error"
            except Exception as e:  # noqa: BLE001
                log.exception("I/O 명령 오류")
                res, status = {"message": f"오류: {e.__class__.__name__}"}, "error"
            if self.command_store:
                self.command_store.finish_command(cid, status, json.dumps(res, ensure_ascii=False))

    # ── 상태 파일 ──────────────────────────────────────────
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "updated_at": self.clock(),
                "buses": [{"id": b.id, "name": b.name, "kind": b.kind, "where": b.where, "enabled": b.enabled}
                          for b in self.buses.values()],
                "devices": [{
                    "id": d.id, "online": d.online, "ever_online": d.ever_online, "error": d.error,
                    "last_ok": d.last_ok, "states": d.states, "commanded": {str(k): v for k, v in d.commanded.items()},
                    "pulse": sorted(d.pulse_until), "points": d.raw_points,
                    "values": {k: v[0] for k, v in d.values.items()},
                } for d in self.devices.values()],
            }

    def _write_status(self, now):
        if now - self._last_status < STATUS_EVERY_SEC:
            return
        self._last_status = now
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError:
            log.exception("I/O 상태 파일 기록 실패")

    # ── 스레드 ─────────────────────────────────────────────
    def start(self):
        for target, name in ((self._poll_loop, "io-poll"), (self._job_loop, "io-jobs")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                self.reload()
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("I/O 주기 오류")
            self._stop.wait(0.5)

    def stop(self):
        """모든 할당 릴레이를 끄고 연결을 닫는다(제어기 정지 뒤 호출)."""
        self._stop.set()
        for t in self._threads:
            t.join(3)
        with self.lock:
            for dev in self.devices.values():
                if dev.kind == "relay" and dev.online:
                    for idx in list(dev.commanded):
                        dev.commanded[idx] = False
                        self._write(dev, idx, False, quiet=True)
            for b in self.buses.values():
                b.t.close()
        log.info("I/O 정지")


class ModbusSensor(Sensor):
    """I/O 관리자가 읽어 둔 값을 측정 루프에 넘긴다."""

    def __init__(self, name, type_, iom: IOManager, simulated: bool):
        super().__init__({"name": name, "interface": "modbus"})
        self.type = type_
        self.iom = iom
        self.simulated = simulated

    def read(self) -> float:
        v = self.iom.sensor_value(self.name)
        if v is None:
            raise RuntimeError(f"Modbus 센서 {self.name} 값 없음(통신 확인)")
        return v
