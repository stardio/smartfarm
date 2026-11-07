"""시스템·구역 정의, 기본 설정, 설정 검증.

시스템 종류(kind):
  irrigation : 정해진 시각에 N분 동안 켬(관수). 동시에 켤 수 있는 구역 수 제한(수압), 대기열.
  switch     : 시간대(스케줄 창) 와/또는 온도 조건(thermo)으로 켜고 끔(조명·공조팬·히터).
  nutrient   : 양액(원수 보충·A/B 액·pH 조절·교반·공급) 수동 운전 + EC/pH 자동 투입.
"""
from __future__ import annotations

import copy
import re

DAYS_ALL = [0, 1, 2, 3, 4, 5, 6]      # 0=월 … 6=일

SYSTEMS = {
    "irrigation": {
        "name": "관수", "kind": "irrigation", "zones": 8, "unit": "구역",
        "max_manual_min": 120,
    },
    "lighting": {
        "name": "조명", "kind": "switch", "zones": 8, "unit": "구역",
        "combine": "window",                 # 스케줄 시간대에만 켬
        "min_on_sec": 0, "min_off_sec": 0, "max_manual_min": 0,   # 0 = 시간 제한 없음
    },
    "fan": {
        "name": "공조팬", "kind": "switch", "zones": 8, "unit": "구역",
        "combine": "or",                     # 스케줄 시간대 이거나 설정온도 초과면 켬
        "thermo_dir": "above", "min_on_sec": 60, "min_off_sec": 60, "max_manual_min": 0,
    },
    "heater": {
        "name": "히터", "kind": "switch", "zones": 8, "unit": "구역",
        "combine": "and",                    # 설정온도 미만이면 켬 (스케줄이 있으면 그 시간대에만)
        "thermo_dir": "below", "require_sensor": True,
        "min_on_sec": 120, "min_off_sec": 120, "max_manual_min": 240,
    },
    "nutrient": {
        "name": "양액", "kind": "nutrient", "zones": 6, "unit": "",
        "channels": ["원수 보충 밸브", "양액 A 펌프", "양액 B 펌프", "pH 조절(산) 펌프", "교반기", "양액 공급 펌프"],
        # 투입 펌프(2~4)는 초 단위 최대 120초, 나머지는 분 단위 최대 120분
        "dosing_channels": [2, 3, 4],
    },
}
ORDER = ["irrigation", "nutrient", "lighting", "fan", "heater"]

NUTRIENT_DEFAULT = {
    "auto": False,
    "ec_sensor": "ec_1", "ec_target": 1.8, "ec_band": 0.2,
    "ph_sensor": "ph_1", "ph_target": 6.0, "ph_band": 0.3,
    "dose_sec": 5, "mix_sec": 300, "max_doses_per_day": 20,
}


def default_zone(system: str, zone: int) -> dict:
    spec = SYSTEMS[system]
    if spec["kind"] == "nutrient":
        return {"name": spec["channels"][zone - 1], "enabled": True}
    cfg = {"name": f"{zone}{spec['unit']}", "enabled": True, "schedules": []}
    if system == "irrigation":
        cfg["flow_lpm"] = 0.0                 # 분당 유량(L) — 입력하면 리포트에 사용량 표시
    if system == "fan":
        cfg["thermo"] = {"enabled": False, "sensor": None, "setpoint": 28.0, "hysteresis": 1.0}
    if system == "heater":
        cfg["thermo"] = {"enabled": True, "sensor": None, "setpoint": 18.0, "hysteresis": 1.0}
        cfg["cutoff"] = 35.0                  # 이 온도 이상이면 무조건 끔(과열 보호)
    return cfg


# ── 검증 ───────────────────────────────────────────────────
class ConfigError(ValueError):
    pass


_HM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def hm_to_min(s: str) -> int:
    m = _HM.fullmatch(s or "")
    if not m:
        raise ConfigError(f"시각 형식 오류: {s!r} (예: 06:30)")
    return int(m.group(1)) * 60 + int(m.group(2))


def _num(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{what}: 숫자를 입력하세요")
    if not lo <= v <= hi:
        raise ConfigError(f"{what}: {lo}~{hi} 범위로 입력하세요")
    return float(v)


def _name(v, default):
    v = str(v if v is not None else default).strip()[:20]
    return v or default


def _days(v):
    if not isinstance(v, list) or not v:
        raise ConfigError("요일을 하나 이상 선택하세요")
    out = sorted({d for d in v if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6})
    if len(out) != len(v):
        raise ConfigError("요일 값 오류")
    return out


def validate_zone(system: str, zone: int, data: dict, sensors: dict[str, str]) -> dict:
    """웹에서 받은 구역 설정을 검사해 저장용으로 정리. sensors = {이름: 종류}."""
    if system not in SYSTEMS:
        raise ConfigError("알 수 없는 시스템")
    spec = SYSTEMS[system]
    if not isinstance(zone, int) or not 1 <= zone <= spec["zones"]:
        raise ConfigError("구역 번호 오류")
    if not isinstance(data, dict):
        raise ConfigError("설정 형식 오류")
    base = default_zone(system, zone)
    out = {"name": _name(data.get("name"), base["name"]), "enabled": bool(data.get("enabled", True))}
    if spec["kind"] == "nutrient":
        return out

    scheds = data.get("schedules") or []
    if not isinstance(scheds, list) or len(scheds) > 12:
        raise ConfigError("스케줄은 12개까지 등록할 수 있습니다")
    clean = []
    for i, s in enumerate(scheds, 1):
        if not isinstance(s, dict):
            raise ConfigError("스케줄 형식 오류")
        item = {"start": s.get("start"), "days": _days(s.get("days")), "enabled": bool(s.get("enabled", True))}
        hm_to_min(item["start"])
        if spec["kind"] == "irrigation":
            item["duration_min"] = _num(s.get("duration_min"), 1, 120, f"스케줄 {i} 관수 시간(분)")
        else:
            item["end"] = s.get("end")
            hm_to_min(item["end"])
        clean.append(item)
    out["schedules"] = clean

    if system == "irrigation":
        out["flow_lpm"] = _num(data.get("flow_lpm", 0) or 0, 0, 1000, "유량(L/분)")
    if "thermo" in base:
        t = data.get("thermo") or {}
        if not isinstance(t, dict):
            raise ConfigError("온도 조건 형식 오류")
        sensor = t.get("sensor") or None
        if sensor is not None and sensors.get(sensor) != "temperature":
            raise ConfigError("온도센서를 목록에서 선택하세요")
        out["thermo"] = {
            "enabled": bool(t.get("enabled", base["thermo"]["enabled"])),
            "sensor": sensor,
            "setpoint": _num(t.get("setpoint", base["thermo"]["setpoint"]), -10, 50, "설정온도"),
            "hysteresis": _num(t.get("hysteresis", base["thermo"]["hysteresis"]), 0.1, 10, "편차"),
        }
    if "cutoff" in base:
        out["cutoff"] = _num(data.get("cutoff", base["cutoff"]), 10, 60, "과열 차단 온도")
    return out


def validate_nutrient(data: dict, sensors: dict[str, str]) -> dict:
    if not isinstance(data, dict):
        raise ConfigError("설정 형식 오류")
    d = NUTRIENT_DEFAULT
    out = {"auto": bool(data.get("auto", False))}
    for key, typ in (("ec_sensor", "ec"), ("ph_sensor", "ph")):
        v = data.get(key) or None
        if v is not None and sensors.get(v) != typ:
            raise ConfigError(f"{typ.upper()} 센서를 목록에서 선택하세요")
        out[key] = v
    out["ec_target"] = _num(data.get("ec_target", d["ec_target"]), 0.1, 6, "목표 EC")
    out["ec_band"] = _num(data.get("ec_band", d["ec_band"]), 0.05, 2, "EC 허용 편차")
    out["ph_target"] = _num(data.get("ph_target", d["ph_target"]), 4, 8, "목표 pH")
    out["ph_band"] = _num(data.get("ph_band", d["ph_band"]), 0.05, 2, "pH 허용 편차")
    out["dose_sec"] = _num(data.get("dose_sec", d["dose_sec"]), 1, 60, "1회 투입 시간(초)")
    out["mix_sec"] = _num(data.get("mix_sec", d["mix_sec"]), 30, 3600, "교반 대기 시간(초)")
    out["max_doses_per_day"] = int(_num(data.get("max_doses_per_day", d["max_doses_per_day"]), 1, 200, "하루 최대 투입 횟수"))
    return out


def merged_zone(system: str, zone: int, stored: dict | None) -> dict:
    cfg = default_zone(system, zone)
    if stored:
        for k, v in stored.items():
            if k == "thermo" and isinstance(v, dict) and "thermo" in cfg:
                cfg["thermo"].update(v)
            else:
                cfg[k] = copy.deepcopy(v)
    return cfg
