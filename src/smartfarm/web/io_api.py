"""웹 ↔ I/O 구성 (버스·장치·출력 할당·센서 할당) API.

구성 저장은 웹서버가 control.db 의 io_* 테이블에 직접 쓰고(측정 서버가 0.5초 안에 다시 읽음),
실제 장비와 통신하는 시운전 명령(릴레이 시험·장치 검색·레지스터 읽기)은 명령 대기열로 측정 서버에 넘긴다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from ..control.store import ControlStore
from ..fieldio.profiles import PROFILES, SENSOR_TYPES
from ..fieldio.store import BAUDS, IOConfigError, IOStore, channel_keys

STALE_SEC = 15


class IOAPI:
    def __init__(self, db_path: Path, status_path: Path, config_path: Path, control_store_fn):
        self.db_path, self.status_path, self.config_path = Path(db_path), Path(status_path), Path(config_path)
        self._store = None
        self._control_store_fn = control_store_fn

    @property
    def store(self) -> IOStore:
        if self._store is None:
            self._store = IOStore(self.db_path)
        return self._store

    @property
    def control_store(self) -> ControlStore:
        return self._control_store_fn()

    def _config_sensors(self) -> set[str]:
        try:
            cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return set()
        return {str(s.get("name")) for s in cfg.get("sensors") or [] if isinstance(s, dict)}

    def live(self) -> dict:
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "unknown", "buses": [], "devices": []}
        raw["state"] = "ok" if time.time() - float(raw.get("updated_at") or 0) <= STALE_SEC else "stale"
        return raw

    def state(self) -> dict:
        return {
            "config": self.store.load(),
            "live": self.live(),
            "profiles": {k: {kk: v[kk] for kk in ("kind", "name", "note") if kk in v} |
                         ({"options": v["options"]} if "options" in v else {}) |
                         ({"points": v["points"]} if "points" in v else {}) for k, v in PROFILES.items()},
            "channels": channel_keys(),
            "sensor_types": list(SENSOR_TYPES),
            "bauds": list(BAUDS),
            "reserved_sensors": sorted(self._config_sensors()),
        }

    def post(self, body: dict) -> tuple[int, dict]:
        op = body.get("op")
        s = self.store
        try:
            if op == "save_bus":
                rid = s.save_bus(body.get("bus") or {}, body.get("id"))
                return 200, {"ok": True, "id": rid, "message": "버스 저장됨"}
            if op == "delete_bus":
                s.delete_bus(int(body.get("id")))
                return 200, {"ok": True, "message": "버스 삭제됨 (소속 장치·할당 포함)"}
            if op == "save_device":
                rid = s.save_device(body.get("device") or {}, body.get("id"))
                return 200, {"ok": True, "id": rid, "message": "장치 저장됨"}
            if op == "delete_device":
                s.delete_device(int(body.get("id")))
                return 200, {"ok": True, "message": "장치 삭제됨 (할당 포함)"}
            if op == "set_map":
                s.set_map(body.get("device_id"), body.get("idx"), body.get("channel") or None)
                return 200, {"ok": True, "message": "출력 할당 저장됨"}
            if op == "save_sensor":
                s.save_sensor(body.get("name"), body.get("device_id"), body.get("point") or {},
                              bool(body.get("enabled", True)), reserved=self._config_sensors())
                return 200, {"ok": True, "message": "센서 할당 저장됨 — 측정값은 다음 측정 주기부터"}
            if op == "delete_sensor":
                s.delete_sensor(str(body.get("name") or ""))
                return 200, {"ok": True, "message": "센서 할당 삭제됨"}
        except IOConfigError as e:
            return 400, {"error": "invalid", "message": str(e)}
        except (TypeError, ValueError):
            return 400, {"error": "invalid", "message": "입력 형식 오류"}
        if op in ("pulse", "scan", "read"):
            return self._job(op, body)
        return 400, {"error": "bad_op", "message": "알 수 없는 요청"}

    def _job(self, op, body) -> tuple[int, dict]:
        if self.live().get("state") != "ok":
            return 503, {"error": "io_down", "message": "측정 서버(I/O 관리자)가 응답하지 않습니다"}
        cmd = {"action": "io_" + op}
        keys = {"pulse": ("device_id", "idx", "seconds"), "scan": ("bus_id", "start", "end"),
                "read": ("device_id", "fn", "addr", "count")}[op]
        for k in keys:
            if k in body:
                cmd[k] = body[k]
        cid = self.control_store.add_command(cmd, source="web")
        return 202, {"ok": True, "command_id": cid, "message": "요청 전달됨"}

    def command_result(self, cid: int) -> dict:
        r = self.control_store.command_result(cid)
        if r is None:
            return {"status": "unknown"}
        out = {"status": r["status"]}
        try:
            data = json.loads(r["result"]) if r["result"] else {}
        except ValueError:
            data = None
        if isinstance(data, dict):
            out.update(data)
        else:
            out["message"] = r["result"]
        return out
