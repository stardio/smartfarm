"""장치 프로파일: 장비 모델별 Modbus 사용법을 데이터로 정의한다.

새 장비는 여기에 프로파일을 추가하거나, 기본 프로파일의 옵션(채널 수·시작 번지 등)을
장치 등록 화면에서 바꿔서 쓴다. 정확한 값은 장비 설명서의 "Modbus 레지스터 표"를 본다.

relay  프로파일 옵션
  channels     릴레이 수
  write        coil(FC05, 여러 개는 FC15) | register(FC06)
  base         첫 릴레이의 번지 (coil: 보통 0, register: 보통 1)
  on_value / off_value   register 방식에서 켜기·끄기 값
  readback     상태 읽기 방식: coil(FC01) | holding(FC03) | none
sensor 프로파일
  points       {이름: {type, fn(3|4), addr, scale, signed}}
"""
from __future__ import annotations

import copy

SENSOR_TYPES = ("temperature", "humidity", "ph", "ec")

PROFILES: dict[str, dict] = {
    "relay_coil": {
        "kind": "relay",
        "name": "릴레이 보드 — 코일 방식 (FC05/FC15, 상태 FC01)",
        "note": "가장 흔한 방식. 1번 릴레이 = 코일 0번지",
        "options": {"channels": 16, "write": "coil", "base": 0, "readback": "coil"},
    },
    "relay_register": {
        "kind": "relay",
        "name": "릴레이 보드 — 레지스터 방식 (FC06, 0x0100 켜기 / 0x0200 끄기)",
        "note": "저가 RS485 릴레이 보드에 많음. 1번 릴레이 = 레지스터 1번지",
        "options": {"channels": 8, "write": "register", "base": 1, "on_value": 256, "off_value": 512,
                    "readback": "holding"},
    },
    "th_sensor": {
        "kind": "sensor",
        "name": "온습도 센서 — 입력 레지스터 1·2 (FC04, ×0.1)",
        "note": "SHT20/SHT30 계열 RS485 온습도 센서(XY-MD02 등)에 흔한 배치",
        "points": {
            "temperature": {"type": "temperature", "fn": 4, "addr": 1, "scale": 0.1, "signed": True},
            "humidity": {"type": "humidity", "fn": 4, "addr": 2, "scale": 0.1, "signed": False},
        },
    },
    "th_sensor_holding": {
        "kind": "sensor",
        "name": "온습도 센서 — 홀딩 레지스터 0·1 (FC03, ×0.1)",
        "note": "다른 흔한 배치. 설명서에서 번지를 확인",
        "points": {
            "temperature": {"type": "temperature", "fn": 3, "addr": 0, "scale": 0.1, "signed": True},
            "humidity": {"type": "humidity", "fn": 3, "addr": 1, "scale": 0.1, "signed": False},
        },
    },
    "custom_sensor": {
        "kind": "sensor",
        "name": "사용자 정의 센서 (번지·배율 직접 입력)",
        "note": "센서 할당 시 기능 코드·번지·배율·부호를 입력",
        "points": {},
    },
}


def profile(pid: str) -> dict | None:
    p = PROFILES.get(pid)
    return copy.deepcopy(p) if p else None


def relay_options(pid: str, options: dict | None) -> dict:
    p = PROFILES[pid]
    out = dict(p["options"])
    for k, v in (options or {}).items():
        if k in out:
            out[k] = v
    return out


def decode(raw: int, scale: float, signed: bool) -> float:
    if signed and raw >= 0x8000:
        raw -= 0x10000
    return round(raw * scale, 3)
