"""자동 경보: 측정값이 기준(rules)을 벗어나거나 센서를 읽지 못하면 텔레그램으로 알린다.

메인 루프가 매 측정 주기마다 AlertManager.update() 를 호출한다.

- 오경보 방지: 연속 N회(기본 3회 = 10초 주기에서 30초) 이탈해야 경보.
- 복구: 기준 안쪽으로 여유값(hysteresis)만큼 들어온 상태가 연속 N회면 복구 알림.
  기준선 근처에서 값이 오르내려도 경보/복구가 반복되지 않는다.
- 지속 알림: 이탈이 계속되면 remind_minutes(기본 60분)마다 다시 알림.
- 센서 오류: 연속 N회 값을 읽지 못하면 경보, 다시 읽히면 복구 알림.
- 모의값 센서(아직 드라이버가 없는 습도/pH/EC)는 기본적으로 제외(include_simulated).
- 정기 보고: heartbeat_hours 마다 온도·경고 요약 전송(0 이면 끔).

설정 (config.yaml → alerts):
    on_threshold_breach: true     # 자동 경보 켜기/끄기
    confirm_count: 3              # 연속 몇 회 이탈 시 경보
    remind_minutes: 60            # 이탈 지속 시 재알림 간격 (0 = 재알림 없음)
    include_simulated: false      # 모의값 센서도 경보할지
    hysteresis: { temperature: 0.5, humidity: 2.0, ph: 0.1, ec: 0.1 }
    heartbeat_hours: 12           # 정기 보고 간격 (0 = 끔)
"""
from __future__ import annotations

import time
from datetime import datetime

from .core.logging import get_logger

log = get_logger(__name__)

TYPE_INFO = {          # 종류 → (이름, 단위, 소수 자리)
    "temperature": ("온도", "°C", 1),
    "humidity": ("습도", "%", 1),
    "ph": ("pH", "", 2),
    "ec": ("EC", "mS/cm", 2),
}
DEFAULT_HYSTERESIS = {"temperature": 0.5, "humidity": 2.0, "ph": 0.1, "ec": 0.1}


def _fmt(v, t) -> str:
    _, unit, dp = TYPE_INFO.get(t, (t, "", 2))
    return f"{v:.{dp}f}{(' ' + unit) if unit else ''}"


def _dur(sec: float) -> str:
    m = int(sec // 60)
    if m < 1:
        return f"{int(sec)}초"
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class _State:
    __slots__ = ("level", "bad", "good", "since", "last_sent", "detail", "fail", "ok", "failed_since")

    def __init__(self):
        self.level = None        # None(정상) | "high" | "low"
        self.bad = 0             # 연속 이탈 횟수
        self.good = 0            # 이탈 중 연속 정상 횟수
        self.since = 0.0         # 이탈 시작 시각
        self.last_sent = 0.0     # 마지막 (재)알림 시각
        self.detail = ""
        self.fail = 0            # 연속 읽기 실패 횟수
        self.ok = 0              # 오류 상태에서 연속 정상 횟수
        self.failed_since = None # 센서 오류 경보 중이면 시작 시각


class AlertManager:
    def __init__(self, cfg: dict, send, clock=time.time, heartbeat_text=None):
        a = cfg.get("alerts") or {}
        self.rules = cfg.get("rules") or {}
        self.enabled = bool(a.get("on_threshold_breach", True))
        self.confirm = max(1, int(a.get("confirm_count", 3)))
        self.remind_sec = max(0, float(a.get("remind_minutes", 60))) * 60
        self.include_simulated = bool(a.get("include_simulated", False))
        self.hyst = dict(DEFAULT_HYSTERESIS, **(a.get("hysteresis") or {}))
        self.heartbeat_sec = max(0, float(a.get("heartbeat_hours", 0) or 0)) * 3600
        self.send = send
        self.clock = clock
        self.heartbeat_text = heartbeat_text
        self.states: dict[str, _State] = {}
        self._next_heartbeat = clock() + self.heartbeat_sec if self.heartbeat_sec else None

    # ── 판단 ───────────────────────────────────────────────
    def _limits(self, type_):
        r = self.rules.get(type_)
        if not isinstance(r, dict):
            return None, None
        return _num(r.get("min")), _num(r.get("max"))

    def _check(self, v, type_):
        """(level, 설명) — level: 'high' | 'low' | None"""
        lo, hi = self._limits(type_)
        if hi is not None and v > hi:
            return "high", f"최대 {_fmt(hi, type_)} 초과"
        if lo is not None and v < lo:
            return "low", f"최소 {_fmt(lo, type_)} 미만"
        return None, ""

    def _recovered(self, v, type_) -> bool:
        lo, hi = self._limits(type_)
        h = _num(self.hyst.get(type_)) or 0
        return (hi is None or v <= hi - h) and (lo is None or v >= lo + h)

    def _emit(self, text: str, out: list):
        out.append(text)
        log.info("경보 전송: %s", text.splitlines()[0])
        try:
            self.send(text)
        except Exception:  # noqa: BLE001
            log.exception("경보 전송 실패")

    # ── 매 주기 호출 ───────────────────────────────────────
    def update(self, sensors, readings: dict) -> list[str]:
        out: list[str] = []
        now = self.clock()
        stamp = datetime.fromtimestamp(now).strftime("%m/%d %H:%M:%S")
        if self.enabled:
            for s in sensors:
                if s.type not in TYPE_INFO:
                    continue
                if getattr(s, "simulated", True) and not self.include_simulated:
                    self.states.pop(s.name, None)
                    continue
                st = self.states.setdefault(s.name, _State())
                self._sensor(s, readings.get(s.name), st, now, stamp, out)
        if self._next_heartbeat is not None and now >= self._next_heartbeat:
            self._next_heartbeat = now + self.heartbeat_sec
            if self.heartbeat_text:
                try:
                    self._emit("🕒 정기 보고\n\n" + self.heartbeat_text(), out)
                except Exception:  # noqa: BLE001
                    log.exception("정기 보고 생성 실패")
        return out

    def _sensor(self, s, value, st: _State, now, stamp, out):
        name = TYPE_INFO[s.type][0]
        v = _num(value)

        # 1) 센서 읽기 실패
        if v is None:
            st.ok = 0
            st.fail += 1
            if st.failed_since is None and st.fail >= self.confirm:
                st.failed_since = now
                self._emit(f"⚠ [센서 오류] {s.name} {name} 값을 읽지 못함\n"
                           f"배선·접촉을 확인하세요. ({stamp})", out)
            return
        st.fail = 0
        if st.failed_since is not None:
            st.ok += 1
            if st.ok >= self.confirm:
                self._emit(f"✅ [센서 복구] {s.name} {name} 정상 측정 {_fmt(v, s.type)}\n"
                           f"(오류 지속 {_dur(now - st.failed_since)}, {stamp})", out)
                st.failed_since = None
                st.ok = 0

        # 2) 기준 이탈
        level, detail = self._check(v, s.type)
        if st.level is None:
            st.bad = st.bad + 1 if level else 0
            if level and st.bad >= self.confirm:
                st.level, st.detail, st.since, st.last_sent, st.good = level, detail, now, now, 0
                self._emit(f"🔴 [경보] {s.name} {name} {_fmt(v, s.type)} — {detail}\n({stamp})", out)
            return

        # 이탈 중
        if self._recovered(v, s.type):
            st.good += 1
            if st.good >= self.confirm:
                self._emit(f"✅ [복구] {s.name} {name} {_fmt(v, s.type)} — 기준 범위로 돌아옴\n"
                           f"(이탈 지속 {_dur(now - st.since)}, {stamp})", out)
                st.level, st.bad, st.good = None, 0, 0
            return
        st.good = 0
        if not level:            # 기준선과 복구선 사이(여유 구간) — 상태 유지, 재알림 안 함
            return
        if level != st.level:    # 반대쪽으로 넘어간 경우(드묾)
            st.level, st.detail = level, detail
        if self.remind_sec and now - st.last_sent >= self.remind_sec:
            st.last_sent = now
            self._emit(f"🔴 [경보 지속] {s.name} {name} {_fmt(v, s.type)} — {st.detail}\n"
                       f"({_dur(now - st.since)}째, {stamp})", out)
