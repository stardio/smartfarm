"""제어기: 1초마다 명령 처리 → 구역별 켜기/끄기 판단 → 출력 적용 → 운전 기록 → 상태 파일.

측정 서버(main.py) 안에서 스레드로 돈다. 측정 루프가 set_readings() 로 최신 센서값을 넘긴다.

안전 원칙
- 시작·종료·전체 정지 시 모든 출력 OFF. 재시작 시 수동 켜기·관수 대기열은 이어가지 않는다.
- 제어기가 꺼져 있던 동안 쌓인(60초 넘은) 명령은 실행하지 않는다.
- 히터: 온도센서가 없거나 값이 오래되면(60초) 끈다. 과열 차단 온도 이상이면 수동 켜기 중이라도 끈다.
- 관수·히터 수동 켜기는 시간 제한 필수(관수 120분, 히터 240분).
- 자동 전환은 최소 켜짐/꺼짐 시간을 지켜 모터·히터가 짧게 반복 동작하지 않게 한다.
- 모의값 센서로는 온도 조건·양액 자동 투입을 하지 않는다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..core.logging import get_logger
from .outputs import build_output
from .store import ControlStore
from .systems import NUTRIENT_DEFAULT, ORDER, SYSTEMS, hm_to_min, merged_zone

log = get_logger(__name__)
ROOT = Path(__file__).resolve().parents[3]
CONTROL_STATUS = ROOT / "data" / "control.json"
SENSOR_MAX_AGE = 60
TICK_SEC = 1.0
STATUS_EVERY_SEC = 10
TRIGGER_NAMES = {"schedule": "스케줄", "manual": "수동", "auto": "자동", "thermo": "온도"}


def in_window(local: datetime, start: str, end: str, days: list[int]) -> bool:
    """start~end 시간대 안인지. end < start 이면 자정을 넘는 시간대(예: 22:00~06:00)."""
    s, e = hm_to_min(start), hm_to_min(end)
    m = local.hour * 60 + local.minute
    wd = local.weekday()
    if s == e:                                     # 하루 종일
        return wd in days
    if s < e:
        return wd in days and s <= m < e
    yesterday = (wd - 1) % 7
    return (m >= s and wd in days) or (m < e and yesterday in days)


class Channel:
    def __init__(self, system: str, zone: int, output):
        self.system, self.zone, self.output = system, zone, output
        self.on = False
        self.since = 0.0
        self.run_id = None
        self.override = None        # {"state": bool, "until": ts|None}
        self.demand = False         # 온도 조건 히스테리시스 상태
        self.reason = ""
        self.end_result = "completed"
        self.sensor = None          # {"name", "value", "ok"}
        self.out_error = ""         # 출력(통신) 실패 메시지


class Controller:
    def __init__(self, cfg: dict, store: ControlStore | None = None, notify=None,
                 clock=time.time, status_path: Path = CONTROL_STATUS, iom=None):
        self.ccfg = cfg.get("control") or {}
        self.iom = iom                       # fieldio.IOManager (없으면 모든 출력 mock)
        irr = self.ccfg.get("irrigation") or {}
        self.max_concurrent = max(1, int(irr.get("max_concurrent", 1)))
        self.use_pump = bool(irr.get("pump", True))
        self.hardware = self.ccfg.get("hardware", "mock")
        self.store = store or ControlStore()
        if iom is not None:
            iom.command_store = self.store
        self.notify = notify or (lambda text: None)
        self.clock = clock
        self.status_path = Path(status_path)
        self.lock = threading.RLock()

        self.channels: dict[tuple[str, int], Channel] = {}
        for sid in ORDER:
            for z in range(1, SYSTEMS[sid]["zones"] + 1):
                key = f"{sid}.{z}"
                self.channels[(sid, z)] = Channel(sid, z, build_output(key, self.ccfg, iom))
        self.pump = Channel("irrigation", 0, build_output("irrigation.pump", self.ccfg, iom)) if self.use_pump else None

        self.readings: dict[str, tuple] = {}      # 이름 → (값, 모의값 여부, 종류, 시각)
        self.paused = False
        self.queue: list[dict] = []                # 관수 대기열
        self.active: dict[int, dict] = {}          # 관수 중 구역 → {"end", "trigger"}
        self.fired: set = set()
        self.nut_auto: dict[int, float] = {}       # 양액 자동 투입으로 켜진 채널 → 끝 시각
        self.nut_phase = {"phase": "idle", "until": 0.0, "msg": ""}
        self.alerted: set[str] = set()
        self.version = -1
        self.zone_cfg: dict[tuple[str, int], dict] = {}
        self.nutrient_cfg = dict(NUTRIENT_DEFAULT)
        self._last_status = 0.0
        self._last_sig = None
        self._stop = threading.Event()
        self._thread = None

        now = self.clock()
        n = self.store.close_open_runs(now)
        if n:
            log.warning("이전 실행에서 끝나지 않은 운전 기록 %d건을 '중단'으로 정리", n)
        for ch in self._all_channels():
            ch.output.set(False)
            ch.since = now
        self._reload(force=True)
        self._premark_fired(now)
        log.info("제어기 준비: 출력 %d개, 드라이버 %s, 관수 동시 %d구역",
                 len(self.channels) + (1 if self.pump else 0), self.hardware, self.max_concurrent)

    # ── 기본 ───────────────────────────────────────────────
    def _all_channels(self):
        yield from self.channels.values()
        if self.pump:
            yield self.pump

    def _reload(self, force=False):
        v = self.store.config_version()
        if v == self.version and not force:
            return
        stored = self.store.all_zone_configs()
        self.zone_cfg = {k: merged_zone(k[0], k[1], stored.get(k)) for k in self.channels}
        self.nutrient_cfg = dict(NUTRIENT_DEFAULT, **(stored.get(("nutrient", 0)) or {}))
        self.version = v

    def set_readings(self, sensors, readings: dict) -> None:
        ts = self.clock()
        with self.lock:
            for s in sensors:
                self.readings[s.name] = (readings.get(s.name), bool(getattr(s, "simulated", True)), s.type, ts)

    def _sensor(self, name, now):
        """(값 또는 None, 상태 dict). 없거나 모의값이거나 60초 넘게 오래되면 값 None."""
        if not name:
            return None, None
        r = self.readings.get(name)
        if r is None:
            return None, {"name": name, "value": None, "ok": False, "why": "없음"}
        value, sim, _type, ts = r
        why = None
        if value is None:
            why = "읽기 실패"
        elif sim:
            why = "모의값"
        elif now - ts > SENSOR_MAX_AGE:
            why = "갱신 중단"
        st = {"name": name, "value": value, "ok": why is None, "why": why}
        return (value if why is None else None), st

    def _alert_once(self, key: str, text: str):
        if key not in self.alerted:
            self.alerted.add(key)
            log.warning("제어 경보: %s", text)
            try:
                self.notify(text)
            except Exception:  # noqa: BLE001
                log.exception("제어 경보 전송 실패")

    def _clear_alert(self, key: str, text: str | None = None):
        if key in self.alerted:
            self.alerted.discard(key)
            if text:
                try:
                    self.notify(text)
                except Exception:  # noqa: BLE001
                    log.exception("제어 경보 전송 실패")

    # ── 출력 적용 + 운전 기록 ──────────────────────────────
    def _apply(self, ch: Channel, desired: bool, trigger: str, now: float, flow_lpm: float = 0.0):
        if desired == ch.on:
            return
        try:
            ch.output.set(desired)
        except Exception as e:  # noqa: BLE001
            if not ch.out_error:                      # 실패가 이어지는 동안 로그는 처음 1회만
                log.error("출력 실패: %s.%s → %s (%s)", ch.system, ch.zone, "ON" if desired else "OFF", e)
            ch.out_error = str(e)[:120]
            return
        if ch.out_error:
            log.info("출력 복구: %s.%s", ch.system, ch.zone)
            ch.out_error = ""
        ch.on, ch.since = desired, now
        if ch.zone == 0:            # 관수 주 펌프: 기록하지 않음
            return
        if desired:
            ch.run_id = self.store.start_run(ch.system, ch.zone, now, trigger)
            ch.end_result = "completed"
        elif ch.run_id is not None:
            self.store.end_run(ch.run_id, now, ch.end_result, flow_lpm)
            ch.run_id = None

    # ── 명령 ───────────────────────────────────────────────
    def _process_commands(self, now):
        for cid, _ts, cmd in self.store.pending_commands(now):
            if str(cmd.get("action", "")).startswith("io_"):
                # 시운전 명령: 릴레이 시험은 전체 정지 상태에서만. 처리는 I/O 작업 스레드가 한다.
                if self.iom is None:
                    self.store.finish_command(cid, "error", json.dumps({"message": "I/O 관리자 없음"}))
                elif cmd["action"] == "io_pulse" and not self.paused:
                    self.store.finish_command(cid, "error", json.dumps(
                        {"message": "릴레이 시험은 '전체 정지' 상태에서만 할 수 있습니다"}, ensure_ascii=False))
                else:
                    self.store.mark_command(cid, "running")
                    self.iom.submit(cid, cmd)
                continue
            try:
                msg = self.execute(cmd, now)
                self.store.finish_command(cid, "done", msg)
            except ValueError as e:
                self.store.finish_command(cid, "error", str(e))

    def execute(self, cmd: dict, now: float | None = None) -> str:
        """명령 실행. 결과 문구 반환, 거부 시 ValueError."""
        now = self.clock() if now is None else now
        action = cmd.get("action")
        if action == "pause":
            self._pause(now)
            return "전체 정지"
        if action == "resume":
            self.paused = False
            self.store.log_event("resume")
            return "자동 운전 재개"
        system, zone = cmd.get("system"), cmd.get("zone")
        if system not in SYSTEMS or not isinstance(zone, int) or (system, zone) not in self.channels:
            raise ValueError("구역 지정 오류")
        spec = SYSTEMS[system]
        ch = self.channels[(system, zone)]
        zc = self.zone_cfg[(system, zone)]
        if action in ("on", "run"):
            if self.paused:
                raise ValueError("전체 정지 중입니다. 먼저 '재개' 하세요")
            if not zc.get("enabled", True):
                raise ValueError("사용 안 함으로 설정된 구역입니다")
            seconds = self._duration(cmd, system, spec, zone)
            if spec["kind"] == "irrigation":
                return self._irrigation_manual(zone, seconds, now)
            ch.override = {"state": True, "until": (now + seconds) if seconds else None}
            return "켜기" + (f" {int(seconds // 60)}분" if seconds and seconds >= 60 else (f" {int(seconds)}초" if seconds else ""))
        if action in ("off", "stop"):
            if spec["kind"] == "irrigation":
                self._irrigation_stop(zone, now, "stopped")
                return "관수 중지"
            if spec["kind"] == "nutrient":
                self.nut_auto.pop(zone, None)
                ch.override = None
                ch.end_result = "stopped"
                return "끄기"
            seconds = self._duration(cmd, system, spec, zone, required=False)
            ch.override = {"state": False, "until": (now + seconds) if seconds else None}
            ch.end_result = "stopped"
            return "끄기"
        if action == "auto":
            ch.override = None
            return "자동"
        raise ValueError("알 수 없는 명령")

    def _duration(self, cmd, system, spec, zone, required=None) -> float | None:
        if spec["kind"] == "nutrient":
            dosing = zone in spec["dosing_channels"]
            if dosing:
                sec = cmd.get("seconds")
                if not isinstance(sec, (int, float)) or isinstance(sec, bool) or not 1 <= sec <= 120:
                    raise ValueError("투입 시간은 1~120초로 입력하세요")
                return float(sec)
            limit, req = 120, True
        else:
            limit = spec.get("max_manual_min", 120)
            req = limit > 0 if required is None else required
            limit = limit or 1440
        m = cmd.get("minutes")
        if m is None:
            if req:
                raise ValueError(f"운전 시간(분)을 입력하세요 (최대 {limit}분)")
            return None
        if not isinstance(m, (int, float)) or isinstance(m, bool) or not 1 <= m <= limit:
            raise ValueError(f"운전 시간은 1~{limit}분으로 입력하세요")
        return float(m) * 60

    def _pause(self, now):
        self.paused = True
        self.queue.clear()
        for z in list(self.active):
            self._irrigation_stop(z, now, "stopped")
        self.nut_auto.clear()
        self.nut_phase = {"phase": "idle", "until": 0.0, "msg": ""}
        for ch in self._all_channels():
            ch.override = None
            ch.end_result = "stopped"
            self._apply(ch, False, "manual", now)
        self.store.log_event("pause")
        log.warning("전체 정지")

    # ── 관수 ───────────────────────────────────────────────
    def _premark_fired(self, now):
        """재시작 직후 같은 분의 스케줄이 다시 실행되지 않게 표시."""
        local = datetime.fromtimestamp(now)
        cur = local.hour * 60 + local.minute
        for z in range(1, SYSTEMS["irrigation"]["zones"] + 1):
            for s in self.zone_cfg[("irrigation", z)].get("schedules", []):
                st = hm_to_min(s["start"])
                if st <= cur < st + 2:
                    self.fired.add((z, s["start"], local.date()))

    def _irrigation_manual(self, zone, seconds, now) -> str:
        if zone in self.active:
            self.active[zone]["end"] = now + seconds
            return f"관수 시간 변경: 지금부터 {int(seconds // 60)}분"
        self.queue = [q for q in self.queue if q["zone"] != zone]
        self.queue.insert(0, {"zone": zone, "seconds": seconds, "trigger": "manual"})
        busy = len(self.active) >= self.max_concurrent
        return f"관수 {int(seconds // 60)}분" + (" (대기열 — 다른 구역 관수 후 시작)" if busy else "")

    def _irrigation_stop(self, zone, now, result):
        self.queue = [q for q in self.queue if q["zone"] != zone]
        if zone in self.active:
            ch = self.channels[("irrigation", zone)]
            ch.end_result = result
            del self.active[zone]
            self._apply(ch, False, "manual", now, self.zone_cfg[("irrigation", zone)].get("flow_lpm", 0))

    def _irrigation(self, now):
        local = datetime.fromtimestamp(now)
        cur = local.hour * 60 + local.minute
        wd = local.weekday()
        for z in range(1, SYSTEMS["irrigation"]["zones"] + 1):
            zc = self.zone_cfg[("irrigation", z)]
            if not zc.get("enabled", True):
                continue
            for s in zc.get("schedules", []):
                if not s.get("enabled", True) or wd not in s["days"]:
                    continue
                st = hm_to_min(s["start"])
                key = (z, s["start"], local.date())
                if st <= cur < st + 2 and key not in self.fired:
                    self.fired.add(key)
                    if z in self.active or any(q["zone"] == z for q in self.queue):
                        self.store.log_event("skip", f"irrigation/{z} {s['start']} 이미 관수 중")
                        continue
                    self.queue.append({"zone": z, "seconds": s["duration_min"] * 60, "trigger": "schedule"})
        # 오래된 표시 정리
        today = local.date()
        self.fired = {k for k in self.fired if k[2] >= today - timedelta(days=1)}

        for z, a in list(self.active.items()):
            if now >= a["end"]:
                self._irrigation_stop(z, now, "completed")
        while self.queue and len(self.active) < self.max_concurrent:
            q = self.queue.pop(0)
            z = q["zone"]
            if not self.zone_cfg[("irrigation", z)].get("enabled", True):
                continue
            self.active[z] = {"end": now + q["seconds"], "trigger": q["trigger"]}
            self._apply(self.channels[("irrigation", z)], True, q["trigger"], now)
        for z in range(1, SYSTEMS["irrigation"]["zones"] + 1):
            ch = self.channels[("irrigation", z)]
            if z in self.active:
                left = self.active[z]["end"] - now
                ch.reason = f"{TRIGGER_NAMES.get(self.active[z]['trigger'], '')} 관수 중 · {int(left // 60)}분 {int(left % 60)}초 남음"
            elif any(q["zone"] == z for q in self.queue):
                ch.reason = "대기 중"
            elif not self.zone_cfg[("irrigation", z)].get("enabled", True):
                ch.reason = "사용 안 함"
            else:
                ch.reason = ""
        if self.pump:
            self._apply(self.pump, bool(self.active), "auto", now)

    def next_irrigation(self, zone, now) -> float | None:
        zc = self.zone_cfg[("irrigation", zone)]
        if not zc.get("enabled", True):
            return None
        base = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
        best = None
        for s in zc.get("schedules", []):
            if not s.get("enabled", True):
                continue
            st = hm_to_min(s["start"])
            for d in range(0, 8):
                day = (base + timedelta(days=d)).replace(hour=st // 60, minute=st % 60)
                if day.weekday() in s["days"] and day.timestamp() > now:
                    t = day.timestamp()
                    best = t if best is None or t < best else best
                    break
        return best

    # ── 조명·공조팬·히터 ───────────────────────────────────
    def _switch(self, system, now):
        spec = SYSTEMS[system]
        local = datetime.fromtimestamp(now)
        for z in range(1, spec["zones"] + 1):
            ch = self.channels[(system, z)]
            zc = self.zone_cfg[(system, z)]
            desired, trigger, reason = self._switch_decide(system, spec, ch, zc, local, now)
            # 자동 전환은 최소 켜짐/꺼짐 시간을 지킨다(수동·안전 정지는 즉시)
            if trigger in ("schedule", "thermo", "auto") and desired != ch.on:
                hold = spec.get("min_on_sec", 0) if ch.on else spec.get("min_off_sec", 0)
                if now - ch.since < hold:
                    desired = ch.on
                    reason += f" (최소 {'켜짐' if ch.on else '꺼짐'} 시간 대기)"
            ch.reason = reason
            self._apply(ch, desired, trigger, now)

    def _switch_decide(self, system, spec, ch, zc, local, now):
        ov = ch.override
        if ov and ov["until"] is not None and now >= ov["until"]:
            ch.override = ov = None
        if not zc.get("enabled", True):
            ch.override = None
            return False, "auto", "사용 안 함"

        t = zc.get("thermo") or {}
        v, ch.sensor = (None, None)
        if t:
            v, ch.sensor = self._sensor(t.get("sensor"), now)

        if system == "heater":
            cutoff = zc.get("cutoff", 35.0)
            if v is not None and v >= cutoff:
                self._alert_once(f"cutoff:{ch.zone}", f"🔥 [히터 과열 차단] {zc['name']} {v:.1f} °C ≥ {cutoff:.0f} °C — 히터를 껐습니다")
                return False, "safety", f"과열 차단 ({v:.1f} °C ≥ {cutoff:.0f} °C)"
            self._clear_alert(f"cutoff:{ch.zone}")

        if ov:
            until = datetime.fromtimestamp(ov["until"]).strftime("%H:%M") if ov["until"] else None
            word = "켜기" if ov["state"] else "끄기"
            return ov["state"], "manual", f"수동 {word}" + (f" ~{until}" if until else "")

        scheds = [s for s in zc.get("schedules", []) if s.get("enabled", True)]
        win = any(in_window(local, s["start"], s["end"], s["days"]) for s in scheds)
        use_thermo = bool(t.get("enabled")) and "thermo_dir" in spec

        demand = False
        if use_thermo:
            if v is None:
                ch.demand = False
                if spec.get("require_sensor") and t.get("sensor"):
                    self._alert_once(f"sensor:{system}:{ch.zone}",
                                     f"⚠ [{spec['name']} 안전 정지] {zc['name']} 온도센서 {t.get('sensor')} 값 없음 — 꺼 둡니다")
            else:
                self._clear_alert(f"sensor:{system}:{ch.zone}",
                                  f"✅ [{spec['name']} 재가동] {zc['name']} 온도센서 정상 ({v:.1f} °C)")
                sp, h = t["setpoint"], t["hysteresis"]
                if spec["thermo_dir"] == "below":        # 난방: sp-h 이하 켬, sp 도달 끔
                    ch.demand = (v < sp) if ch.demand else (v <= sp - h)
                else:                                     # 냉방·환기: sp+h 이상 켬, sp 이하 끔
                    ch.demand = (v > sp) if ch.demand else (v >= sp + h)
            demand = ch.demand

        combine = spec["combine"]
        temp_txt = f"{v:.1f} °C / 설정 {t['setpoint']:.1f} °C" if use_thermo and v is not None else ""
        if use_thermo and v is None:
            temp_txt = "온도센서 " + ("미지정" if not t.get("sensor") else (ch.sensor or {}).get("why", "없음"))

        if combine == "window":
            return win, "schedule", ("스케줄 시간대" if win else ("스케줄 없음" if not scheds else "스케줄 외 시간"))
        if combine == "or":
            if win:
                return True, "schedule", "스케줄 시간대"
            if use_thermo:
                return demand, "thermo", ("온도 높음 · " if demand else "") + temp_txt
            return False, "auto", "스케줄 없음" if not scheds else "스케줄 외 시간"
        # and (히터)
        if not use_thermo:
            return False, "auto", "온도 조건 꺼짐"
        if scheds and not win:
            return False, "auto", "스케줄 외 시간 · " + temp_txt
        if v is None:
            return False, "safety" if t.get("sensor") else "auto", temp_txt + (" — 안전 정지" if t.get("sensor") else "")
        return demand, "thermo", ("난방 중 · " if demand else "") + temp_txt

    # ── 양액 ───────────────────────────────────────────────
    def _nutrient(self, now):
        spec = SYSTEMS["nutrient"]
        nc = self.nutrient_cfg
        ph = self.nut_phase
        # 자동 투입 상태기계
        if not nc.get("auto"):
            self.nut_auto.clear()
            ph.update(phase="idle", until=0.0, msg="자동 투입 꺼짐")
        elif ph["phase"] in ("dosing", "mixing") and now < ph["until"]:
            pass
        elif ph["phase"] == "dosing":
            ph.update(phase="mixing", until=now + nc["mix_sec"], msg="교반 대기 (투입 후 섞이는 시간)")
        else:
            ph["phase"] = "idle"
            ec, ec_st = self._sensor(nc.get("ec_sensor"), now)
            p, p_st = self._sensor(nc.get("ph_sensor"), now)
            if ec is None or p is None:
                bad = [st for st in (ec_st, p_st) if not st or not st["ok"]]
                why = ", ".join(f"{st['name']} {st['why']}" for st in bad if st) or "센서 미지정"
                ph["msg"] = f"자동 투입 대기 — {why}"
            elif self.store.doses_today(now) >= nc["max_doses_per_day"]:
                ph["msg"] = "오늘 최대 투입 횟수 도달 — 자동 투입 중지"
                self._alert_once(f"dose_max:{datetime.fromtimestamp(now).date()}",
                                 f"⚠ [양액] 오늘 자동 투입 최대 횟수({nc['max_doses_per_day']}회) 도달 — 탱크·센서를 확인하세요")
            elif ec < nc["ec_target"] - nc["ec_band"]:
                self._dose([2, 3], now, nc)
                ph.update(phase="dosing", until=now + nc["dose_sec"], msg=f"EC {ec:.2f} 낮음 → A·B 액 {nc['dose_sec']:.0f}초 투입")
            elif p > nc["ph_target"] + nc["ph_band"]:
                self._dose([4], now, nc)
                ph.update(phase="dosing", until=now + nc["dose_sec"], msg=f"pH {p:.2f} 높음 → 산 {nc['dose_sec']:.0f}초 투입")
            else:
                msg = f"정상 · EC {ec:.2f} · pH {p:.2f}"
                if ec > nc["ec_target"] + nc["ec_band"]:
                    msg = f"EC {ec:.2f} 높음 — 원수 보충 필요"
                    self._alert_once("ec_high", f"⚠ [양액] EC {ec:.2f} 높음 (목표 {nc['ec_target']}) — 원수 보충이 필요합니다")
                else:
                    self._clear_alert("ec_high")
                if p < nc["ph_target"] - nc["ph_band"]:
                    msg = f"pH {p:.2f} 낮음 — 확인 필요"
                ph["msg"] = msg

        for z in range(1, spec["zones"] + 1):
            ch = self.channels[("nutrient", z)]
            zc = self.zone_cfg[("nutrient", z)]
            ov = ch.override
            if ov and ov["until"] is not None and now >= ov["until"]:
                ch.override = ov = None
            auto_until = self.nut_auto.get(z)
            if auto_until is not None and now >= auto_until:
                self.nut_auto.pop(z, None)
                auto_until = None
            if not zc.get("enabled", True):
                ch.override = None
                self.nut_auto.pop(z, None)
                desired, trig, reason = False, "auto", "사용 안 함"
            elif ov:
                left = (ov["until"] - now) if ov["until"] else 0
                desired, trig = ov["state"], "manual"
                reason = f"수동 운전 · {int(left)}초 남음" if left < 120 else f"수동 운전 · {int(left // 60)}분 남음"
            elif auto_until is not None:
                desired, trig, reason = True, "auto", f"자동 투입 · {int(auto_until - now)}초 남음"
            else:
                desired, trig, reason = False, "auto", ""
            ch.reason = reason
            self._apply(ch, desired, trig, now)

    def _dose(self, pumps, now, nc):
        for z in pumps:
            if self.zone_cfg[("nutrient", z)].get("enabled", True):
                self.nut_auto[z] = now + nc["dose_sec"]
        if self.zone_cfg[("nutrient", 5)].get("enabled", True):          # 교반기
            self.nut_auto[5] = now + nc["dose_sec"] + nc["mix_sec"]
        self.store.log_event("dose", ",".join(map(str, pumps)), ts=now)

    # ── 한 주기 ────────────────────────────────────────────
    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            self._reload()
            self._process_commands(now)
            if self.paused:
                for ch in self._all_channels():
                    ch.reason = "전체 정지"
                    self._apply(ch, False, "manual", now)
            else:
                self._irrigation(now)
                self._nutrient(now)
                for sid in ("lighting", "fan", "heater"):
                    self._switch(sid, now)
            self._write_status(now)

    def snapshot(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        systems = []
        for sid in ORDER:
            spec = SYSTEMS[sid]
            zones = []
            for z in range(1, spec["zones"] + 1):
                ch = self.channels[(sid, z)]
                zc = self.zone_cfg[(sid, z)]
                item = {
                    "zone": z, "name": zc.get("name", str(z)), "enabled": bool(zc.get("enabled", True)),
                    "on": ch.on, "since": ch.since, "reason": ch.reason,
                    "mode": "manual" if ch.override else "auto",
                    "override": dict(ch.override) if ch.override else None,
                }
                if ch.sensor:
                    item["sensor"] = ch.sensor
                if self.iom is not None:
                    item["io"] = self.iom.channel_info(f"{sid}.{z}")
                if ch.out_error:
                    item["out_error"] = ch.out_error
                if sid == "irrigation":
                    if z in self.active:
                        item["end"] = self.active[z]["end"]
                        item["mode"] = "manual" if self.active[z]["trigger"] == "manual" else "auto"
                    item["queued"] = any(q["zone"] == z for q in self.queue)
                    item["next"] = self.next_irrigation(z, now)
                zones.append(item)
            entry = {"id": sid, "name": spec["name"], "kind": spec["kind"], "zones": zones,
                     "on_count": sum(1 for x in zones if x["on"])}
            if sid == "irrigation":
                entry["pump"] = bool(self.pump and self.pump.on)
                entry["queue"] = [q["zone"] for q in self.queue]
                entry["max_concurrent"] = self.max_concurrent
            if sid == "nutrient":
                entry["auto"] = bool(self.nutrient_cfg.get("auto"))
                entry["phase"] = dict(self.nut_phase)
            systems.append(entry)
        hardware = self.hardware
        if self.iom is not None:
            hardware = "modbus" if self.iom.has_real_outputs() else ("modbus-sim" if self.iom.map else "mock")
        return {"updated_at": now, "paused": self.paused, "hardware": hardware, "systems": systems}

    def _write_status(self, now):
        sig = (self.paused, tuple(ch.on for ch in self._all_channels()),
               tuple(bool(ch.override) for ch in self.channels.values()), len(self.queue))
        if sig == self._last_sig and now - self._last_status < STATUS_EVERY_SEC:
            return
        self._last_sig, self._last_status = sig, now
        data = self.snapshot(now)
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.status_path)
        except OSError:
            log.exception("제어 상태 파일 기록 실패")

    # ── 스레드 ─────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="controller", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("제어 주기 오류")
            self._stop.wait(max(0.05, TICK_SEC - (time.monotonic() - started)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(5)
        with self.lock:
            now = self.clock()
            for ch in self._all_channels():
                ch.end_result = "stopped"
                flow = self.zone_cfg.get((ch.system, ch.zone), {}).get("flow_lpm", 0) if ch.system == "irrigation" else 0
                self._apply(ch, False, "manual", now, flow)
                try:
                    ch.output.close()
                except Exception:  # noqa: BLE001
                    log.exception("출력 정리 실패")
            self.active.clear()
            self.queue.clear()
            self._last_sig = None
            self._write_status(now)
        log.info("제어기 정지: 모든 출력 OFF")
