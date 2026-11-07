"""웹 ↔ 제어기 연결 (구역 제어 API).

웹서버는 출력을 직접 건드리지 않는다. 명령은 control.db 대기열에 넣고, 측정 서버 안의
제어기가 1초 안에 처리한다. 상태는 제어기가 쓰는 data/control.json 을 읽는다.

- 공개: /api/public/control  → 시스템별 구역 이름과 켜짐/꺼짐만
- 관리자(로그인 필요): 상태·설정·리포트 조회, 명령, 설정 저장
- 실제 출력 드라이버(mock 아님)를 쓰는 동안에는 PIN 로그인으로 제어할 수 없다(10자 이상 비밀번호 필요).
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from ..control.store import ControlStore
from ..control.systems import (NUTRIENT_DEFAULT, ORDER, SYSTEMS, ConfigError, merged_zone,
                               validate_nutrient, validate_zone)

STALE_SEC = 30
COMMAND_WAIT_SEC = 3.0
REPORT_DAYS = (1, 7, 30, 90)
TRIGGERS = {"schedule": "스케줄", "manual": "수동", "auto": "자동", "thermo": "온도", "safety": "안전"}
RESULTS = {"completed": "완료", "stopped": "중지", "interrupted": "중단(재시작)", None: "진행 중"}


class ControlAPI:
    def __init__(self, db_path: Path, status_path: Path, config_path: Path):
        self.db_path, self.status_path, self.config_path = Path(db_path), Path(status_path), Path(config_path)
        self._store = None

    @property
    def store(self) -> ControlStore:
        if self._store is None:
            self._store = ControlStore(self.db_path)
        return self._store

    # ── 읽기 ───────────────────────────────────────────────
    def _cfg(self) -> dict:
        try:
            return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def sensors(self) -> dict[str, str]:
        out = {}
        for s in self._cfg().get("sensors") or []:
            if isinstance(s, dict) and s.get("name"):
                out[str(s["name"])] = str(s.get("type", ""))
        return out

    def hardware_real(self) -> bool:
        c = self._cfg().get("control") or {}
        if c.get("hardware", "mock") != "mock":
            return True
        return any((v or {}).get("driver", "mock") != "mock" for v in (c.get("channels") or {}).values())

    def status(self) -> dict:
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "unknown", "systems": [], "paused": False}
        age = time.time() - float(raw.get("updated_at") or 0)
        raw["state"] = "ok" if age <= STALE_SEC else "stale"
        return raw

    def public(self) -> dict:
        st = self.status()
        systems = []
        for s in st.get("systems") or []:
            if s.get("id") not in SYSTEMS:
                continue
            systems.append({
                "id": s["id"], "name": SYSTEMS[s["id"]]["name"],
                "zones": [{"zone": int(z.get("zone", 0)), "name": str(z.get("name", ""))[:20],
                           "on": bool(z.get("on")), "enabled": bool(z.get("enabled", True))}
                          for z in s.get("zones") or []],
            })
        return {"state": st.get("state", "unknown"), "paused": bool(st.get("paused")), "systems": systems}

    def admin_state(self, pin_login: bool) -> dict:
        stored = self.store.all_zone_configs()
        configs = {sid: [merged_zone(sid, z, stored.get((sid, z))) for z in range(1, SYSTEMS[sid]["zones"] + 1)]
                   for sid in ORDER}
        real = self.hardware_real()
        return {
            "status": self.status(),
            "systems": [{"id": sid, **{k: v for k, v in SYSTEMS[sid].items()}} for sid in ORDER],
            "configs": configs,
            "nutrient": dict(NUTRIENT_DEFAULT, **(stored.get(("nutrient", 0)) or {})),
            "sensors": self.sensors(),
            "hardware_real": real,
            "control_allowed": not (real and pin_login),
        }

    def report(self, days: int) -> dict:
        rep = self.store.daily_report(days)
        rep["runs"] = self.store.recent_runs(200)
        stored = self.store.all_zone_configs()
        rep["names"] = {sid: {z: merged_zone(sid, z, stored.get((sid, z)))["name"]
                              for z in range(1, SYSTEMS[sid]["zones"] + 1)} for sid in ORDER}
        return rep

    def report_csv(self, days: int) -> bytes:
        since = time.time() - days * 86400
        runs = [r for r in self.store.recent_runs(100000) if r["start_ts"] >= since]
        stored = self.store.all_zone_configs()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["시스템", "구역", "구역 이름", "시작", "종료", "운전 시간(분)", "방식", "결과", "사용량(L)"])
        for r in reversed(runs):
            sid = r["system"]
            name = merged_zone(sid, r["zone"], stored.get((sid, r["zone"])))["name"] if sid in SYSTEMS else ""
            fmt = lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""  # noqa: E731
            w.writerow([SYSTEMS.get(sid, {}).get("name", sid), r["zone"], name, fmt(r["start_ts"]), fmt(r["end_ts"]),
                        round((r["duration_sec"] or 0) / 60, 1), TRIGGERS.get(r["trigger"], r["trigger"]),
                        RESULTS.get(r["result"], r["result"]), r["liters"] if r["liters"] is not None else ""])
        return ("﻿" + buf.getvalue()).encode("utf-8")        # BOM: 엑셀에서 한글 깨짐 방지

    # ── 쓰기 ───────────────────────────────────────────────
    def command(self, body: dict) -> tuple[int, dict]:
        action = body.get("action")
        if action not in ("on", "off", "auto", "pause", "resume"):
            return 400, {"error": "bad_action", "message": "알 수 없는 명령"}
        payload = {"action": action}
        if action not in ("pause", "resume"):
            sid, zone = body.get("system"), body.get("zone")
            if sid not in SYSTEMS or not isinstance(zone, int) or not 1 <= zone <= SYSTEMS[sid]["zones"]:
                return 400, {"error": "bad_zone", "message": "구역 지정 오류"}
            payload.update(system=sid, zone=zone)
            for k in ("minutes", "seconds"):
                v = body.get(k)
                if v is not None:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        return 400, {"error": "bad_value", "message": "시간은 숫자로 입력하세요"}
                    payload[k] = v
        if self.status().get("state") != "ok":
            return 503, {"error": "controller_down", "message": "제어기(측정 서버)가 응답하지 않습니다"}
        cid = self.store.add_command(payload, source="web")
        end = time.monotonic() + COMMAND_WAIT_SEC
        while time.monotonic() < end:
            res = self.store.command_result(cid)
            if res and res["status"] != "pending":
                ok = res["status"] == "done"
                return (200 if ok else 400), {"ok": ok, "message": res["result"]}
            time.sleep(0.2)
        return 202, {"ok": True, "message": "명령 전달됨 (처리 대기)"}

    def save_config(self, body: dict) -> tuple[int, dict]:
        sid, zone, data = body.get("system"), body.get("zone"), body.get("config")
        try:
            if sid == "nutrient" and zone == 0:
                clean = validate_nutrient(data, self.sensors())
            else:
                clean = validate_zone(sid, zone, data, self.sensors())
        except ConfigError as e:
            return 400, {"error": "invalid", "message": str(e)}
        self.store.save_zone_config(sid, zone, clean, source="web")
        return 200, {"ok": True, "config": clean}
