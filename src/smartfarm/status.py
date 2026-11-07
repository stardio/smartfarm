"""현재 상태 스냅샷(data/status.json) 기록.

메인 루프가 매 측정 주기마다 호출한다. 공개 웹서버(smartfarm.web)는 이 파일과
SQLite DB 만 읽으므로 메인 서버와 프로세스가 분리된다.

여기에 쓰는 값은 공개될 수 있으므로 GPIO 핀, 센서 주소, 비밀값은 넣지 않는다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATUS_PATH = ROOT / "data" / "status.json"


def build_status(cfg: dict, sensors, readings: dict, valves) -> dict:
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "interval_sec": cfg.get("sampling_interval_sec", 30),
        "sensors": [
            {
                "name": s.name,
                "type": s.type,
                "value": readings.get(s.name),
                "simulated": bool(getattr(s, "simulated", True)),
            }
            for s in sensors
        ],
        "valves": [
            {"name": v.name, "role": v.role, "open": bool(v.is_open)} for v in valves
        ],
        "rules": cfg.get("rules", {}),
    }


def write_status(cfg: dict, sensors, readings: dict, valves, path: Path = STATUS_PATH) -> None:
    """원자적으로 기록(임시 파일 → rename)해 읽는 쪽이 반쯤 쓴 파일을 보지 않게 한다."""
    data = build_status(cfg, sensors, readings, valves)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
