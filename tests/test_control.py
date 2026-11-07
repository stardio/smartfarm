"""구역 제어 테스트 (모의 출력, 가짜 시계).

실행:  PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_control
"""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

from smartfarm.control.engine import Controller, in_window
from smartfarm.control.store import ControlStore
from smartfarm.control.systems import ConfigError, validate_zone
from smartfarm.web import admin
from smartfarm.web.public_server import make_server

SENSORS = {"temp_1": "temperature", "ec_1": "ec", "ph_1": "ph"}


class Clock:
    def __init__(self, dt: datetime):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t

    def add(self, sec):
        self.t += sec


class S:
    def __init__(self, name, type_, simulated=False):
        self.name, self.type, self.simulated = name, type_, simulated


class Base(unittest.TestCase):
    start = datetime(2026, 11, 9, 5, 59, 0)       # 월요일

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = ControlStore(d / "control.db")
        self.clock = Clock(self.start)
        self.sent = []
        self.cfg = {"control": {"hardware": "mock", "irrigation": {"max_concurrent": 1, "pump": True}}}
        self.ctl = None
        self.status = d / "control.json"

    def make(self):
        self.ctl = Controller(self.cfg, self.store, self.sent.append, clock=self.clock, status_path=self.status)
        return self.ctl

    def tearDown(self):
        self.tmp.cleanup()

    def run_for(self, sec, step=1):
        for _ in range(int(sec / step)):
            self.clock.add(step)
            self.ctl.tick()

    def zone(self, system, z):
        return self.ctl.channels[(system, z)]

    def save(self, system, z, cfg):
        self.store.save_zone_config(system, z, validate_zone(system, z, cfg, SENSORS))


class WindowTest(unittest.TestCase):
    def test_windows(self):
        mon = datetime(2026, 11, 9, 23, 0)
        self.assertTrue(in_window(mon, "22:00", "06:00", [0]))
        tue_early = datetime(2026, 11, 10, 5, 0)
        self.assertTrue(in_window(tue_early, "22:00", "06:00", [0]))     # 월요일 밤 시작분
        self.assertFalse(in_window(tue_early, "22:00", "06:00", [1]))
        self.assertTrue(in_window(datetime(2026, 11, 9, 12, 0), "06:00", "18:00", [0]))
        self.assertFalse(in_window(datetime(2026, 11, 9, 18, 0), "06:00", "18:00", [0]))
        self.assertTrue(in_window(datetime(2026, 11, 9, 3, 0), "00:00", "00:00", [0]))

    def test_validation(self):
        with self.assertRaises(ConfigError):
            validate_zone("irrigation", 1, {"schedules": [{"start": "25:00", "duration_min": 5, "days": [0]}]}, SENSORS)
        with self.assertRaises(ConfigError):
            validate_zone("irrigation", 1, {"schedules": [{"start": "06:00", "duration_min": 500, "days": [0]}]}, SENSORS)
        with self.assertRaises(ConfigError):
            validate_zone("heater", 1, {"thermo": {"sensor": "ph_1"}}, SENSORS)
        with self.assertRaises(ConfigError):
            validate_zone("lighting", 9, {}, SENSORS)


class IrrigationTest(Base):
    def test_schedule_runs_sequentially_with_pump(self):
        for z in (1, 2):
            self.save("irrigation", z, {"schedules": [{"start": "06:00", "duration_min": 2, "days": [0]}],
                                        "flow_lpm": 10})
        self.make()
        self.run_for(61)                                   # 06:00:01
        self.assertTrue(self.zone("irrigation", 1).on)
        self.assertFalse(self.zone("irrigation", 2).on)    # 동시 1구역 → 대기
        self.assertTrue(self.ctl.pump.on)
        self.assertEqual([q["zone"] for q in self.ctl.queue], [2])
        self.run_for(120)
        self.assertFalse(self.zone("irrigation", 1).on)
        self.assertTrue(self.zone("irrigation", 2).on)
        self.run_for(121)
        self.assertFalse(self.zone("irrigation", 2).on)
        self.assertFalse(self.ctl.pump.on)
        runs = self.store.recent_runs()
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["result"] == "completed" and r["trigger"] == "schedule" for r in runs))
        self.assertAlmostEqual(runs[0]["liters"], 20.0, delta=0.5)
        self.run_for(300)                                  # 같은 날 다시 실행되지 않음
        self.assertEqual(len(self.store.recent_runs()), 2)

    def test_schedule_not_on_other_day(self):
        self.save("irrigation", 1, {"schedules": [{"start": "06:00", "duration_min": 2, "days": [2]}]})
        self.make()
        self.run_for(120)
        self.assertFalse(self.zone("irrigation", 1).on)

    def test_manual_requires_duration_and_stop(self):
        self.make()
        with self.assertRaises(ValueError):
            self.ctl.execute({"action": "on", "system": "irrigation", "zone": 3})
        self.ctl.execute({"action": "on", "system": "irrigation", "zone": 3, "minutes": 5})
        self.run_for(2)
        self.assertTrue(self.zone("irrigation", 3).on)
        self.ctl.execute({"action": "off", "system": "irrigation", "zone": 3})
        self.run_for(1)
        self.assertFalse(self.zone("irrigation", 3).on)
        self.assertEqual(self.store.recent_runs()[0]["result"], "stopped")

    def test_disabled_zone_rejected(self):
        self.save("irrigation", 4, {"enabled": False})
        self.make()
        with self.assertRaises(ValueError):
            self.ctl.execute({"action": "on", "system": "irrigation", "zone": 4, "minutes": 5})


class SwitchTest(Base):
    def test_lighting_schedule(self):
        self.save("lighting", 1, {"schedules": [{"start": "06:00", "end": "06:05", "days": [0]}]})
        self.make()
        self.run_for(30)
        self.assertFalse(self.zone("lighting", 1).on)
        self.run_for(40)
        self.assertTrue(self.zone("lighting", 1).on)
        self.run_for(300)
        self.assertFalse(self.zone("lighting", 1).on)

    def test_manual_override_and_auto(self):
        self.make()
        self.ctl.execute({"action": "on", "system": "lighting", "zone": 2})
        self.run_for(5)
        self.assertTrue(self.zone("lighting", 2).on)
        self.ctl.execute({"action": "auto", "system": "lighting", "zone": 2})
        self.run_for(1)
        self.assertFalse(self.zone("lighting", 2).on)

    def test_heater_thermostat_with_hysteresis_and_min_time(self):
        self.save("heater", 1, {"thermo": {"enabled": True, "sensor": "temp_1", "setpoint": 18, "hysteresis": 1}})
        self.make()
        t = S("temp_1", "temperature")
        self.run_for(130)                                  # 최소 꺼짐 시간 경과
        for v, expect in ((17.5, False), (16.9, True), (17.8, True), (18.1, False)):
            for _ in range(13):                            # 측정 루프처럼 10초마다 새 값
                self.ctl.set_readings([t], {"temp_1": v})
                self.run_for(10)
            self.assertEqual(self.zone("heater", 1).on, expect, v)

    def test_heater_failsafe_without_sensor(self):
        self.save("heater", 1, {"thermo": {"enabled": True, "sensor": "temp_1", "setpoint": 18, "hysteresis": 1}})
        self.make()
        t = S("temp_1", "temperature")
        self.run_for(130)
        self.ctl.set_readings([t], {"temp_1": 15.0})
        self.run_for(2)
        self.assertTrue(self.zone("heater", 1).on)
        self.ctl.set_readings([t], {})                     # 읽기 실패
        self.run_for(2)
        self.assertFalse(self.zone("heater", 1).on)        # 최소 켜짐 시간과 무관하게 즉시
        self.assertTrue(any("안전 정지" in m for m in self.sent))

    def test_heater_stale_and_simulated_sensor(self):
        self.save("heater", 1, {"thermo": {"enabled": True, "sensor": "temp_1", "setpoint": 18, "hysteresis": 1}})
        self.make()
        self.run_for(130)
        self.ctl.set_readings([S("temp_1", "temperature", simulated=True)], {"temp_1": 10.0})
        self.run_for(2)
        self.assertFalse(self.zone("heater", 1).on)
        self.ctl.set_readings([S("temp_1", "temperature")], {"temp_1": 10.0})
        self.run_for(2)
        self.assertTrue(self.zone("heater", 1).on)
        self.run_for(70)                                    # 값 갱신이 60초 넘게 멈춤
        self.assertFalse(self.zone("heater", 1).on)

    def test_heater_cutoff_beats_manual(self):
        self.save("heater", 1, {"thermo": {"enabled": True, "sensor": "temp_1", "setpoint": 18, "hysteresis": 1},
                                "cutoff": 35})
        self.make()
        with self.assertRaises(ValueError):                 # 히터 수동은 시간 필수
            self.ctl.execute({"action": "on", "system": "heater", "zone": 1})
        self.ctl.execute({"action": "on", "system": "heater", "zone": 1, "minutes": 30})
        self.ctl.set_readings([S("temp_1", "temperature")], {"temp_1": 20.0})
        self.run_for(2)
        self.assertTrue(self.zone("heater", 1).on)
        self.ctl.set_readings([S("temp_1", "temperature")], {"temp_1": 36.0})
        self.run_for(1)
        self.assertFalse(self.zone("heater", 1).on)
        self.assertTrue(any("과열 차단" in m for m in self.sent))

    def test_fan_or_logic(self):
        self.save("fan", 1, {"thermo": {"enabled": True, "sensor": "temp_1", "setpoint": 28, "hysteresis": 1}})
        self.make()
        t = S("temp_1", "temperature")
        self.run_for(70)
        self.ctl.set_readings([t], {"temp_1": 29.5})
        self.run_for(2)
        self.assertTrue(self.zone("fan", 1).on)
        self.ctl.set_readings([t], {"temp_1": 27.5})
        self.run_for(2)
        self.assertTrue(self.zone("fan", 1).on)             # 최소 켜짐 60초
        self.run_for(70)
        self.assertFalse(self.zone("fan", 1).on)


class PauseNutrientTest(Base):
    def test_pause_turns_everything_off(self):
        self.make()
        self.ctl.execute({"action": "on", "system": "lighting", "zone": 1})
        self.ctl.execute({"action": "on", "system": "irrigation", "zone": 1, "minutes": 5})
        self.run_for(2)
        self.ctl.execute({"action": "pause"})
        self.run_for(1)
        self.assertFalse(any(ch.on for ch in self.ctl.channels.values()))
        with self.assertRaises(ValueError):
            self.ctl.execute({"action": "on", "system": "lighting", "zone": 1})
        self.ctl.execute({"action": "resume"})
        self.assertFalse(self.ctl.paused)

    def test_nutrient_manual_dose_limits(self):
        self.make()
        with self.assertRaises(ValueError):
            self.ctl.execute({"action": "on", "system": "nutrient", "zone": 2, "seconds": 500})
        self.ctl.execute({"action": "on", "system": "nutrient", "zone": 2, "seconds": 5})
        self.run_for(1)
        self.assertTrue(self.zone("nutrient", 2).on)
        self.run_for(5)
        self.assertFalse(self.zone("nutrient", 2).on)

    def test_nutrient_auto_dosing(self):
        self.store.save_zone_config("nutrient", 0, {"auto": True, "ec_sensor": "ec_1", "ph_sensor": "ph_1",
                                                    "ec_target": 1.8, "ec_band": 0.2, "ph_target": 6.0,
                                                    "ph_band": 0.3, "dose_sec": 5, "mix_sec": 60,
                                                    "max_doses_per_day": 2})
        self.make()
        sens = [S("ec_1", "ec"), S("ph_1", "ph")]
        self.ctl.set_readings(sens, {"ec_1": 1.2, "ph_1": 6.0})
        self.run_for(1)
        self.assertTrue(self.zone("nutrient", 2).on and self.zone("nutrient", 3).on)
        self.assertTrue(self.zone("nutrient", 5).on)        # 교반기
        self.run_for(6)
        self.assertFalse(self.zone("nutrient", 2).on)
        self.ctl.set_readings(sens, {"ec_1": 1.2, "ph_1": 6.0})
        self.run_for(30)
        self.assertFalse(self.zone("nutrient", 2).on)       # 교반 대기 중에는 재투입 없음
        for _ in range(3):
            self.ctl.set_readings(sens, {"ec_1": 1.2, "ph_1": 6.0})
            self.run_for(70)
        self.assertEqual(self.store.doses_today(self.clock()), 2)       # 하루 최대
        self.assertTrue(any("최대" in m for m in self.sent))

    def test_nutrient_auto_blocked_with_simulated_sensors(self):
        self.store.save_zone_config("nutrient", 0, {"auto": True})
        self.make()
        self.ctl.set_readings([S("ec_1", "ec", True), S("ph_1", "ph", True)], {"ec_1": 0.5, "ph_1": 8.0})
        self.run_for(3)
        self.assertFalse(any(self.zone("nutrient", z).on for z in range(1, 7)))
        self.assertIn("모의값", self.ctl.nut_phase["msg"])


class RestartTest(Base):
    def test_open_runs_closed_and_outputs_off_on_stop(self):
        self.make()
        self.ctl.execute({"action": "on", "system": "lighting", "zone": 1})
        self.run_for(10)
        self.ctl.stop()
        self.assertFalse(self.zone("lighting", 1).on)
        self.assertEqual(self.store.recent_runs()[0]["result"], "stopped")
        snap = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertFalse(any(z["on"] for s in snap["systems"] for z in s["zones"]))

    def test_expired_commands_ignored(self):
        self.make()
        cid = self.store.add_command({"action": "on", "system": "lighting", "zone": 1})
        self.store.pending_commands(time.time() + 120)      # 2분 뒤 처리 → 만료
        self.assertEqual(self.store.command_result(cid)["status"], "expired")


class WebTest(unittest.TestCase):
    """웹 API: 로그인 필요, 명령이 제어기에 전달되고 결과가 돌아오는지."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "config.yaml").write_text(
            "sensors:\n  - { name: temp_1, type: temperature }\ncontrol:\n  hardware: mock\n", encoding="utf-8")
        self.cdb, self.cjson = d / "control.db", d / "control.json"
        self.ctl = Controller({"control": {"hardware": "mock"}}, ControlStore(self.cdb), status_path=self.cjson)
        self.ctl.start()
        time.sleep(1.2)
        self.httpd = make_server("127.0.0.1", 0, d / "x.db", d / "s.json", admin_hash=admin.hash_password("1234"),
                                 config_path=d / "config.yaml", log_path=d / "x.log", admin_kind="pin",
                                 control_db=self.cdb, control_status=self.cjson)
        self.httpd.RequestHandlerClass.auth.FAIL_DELAY_SEC = 0
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.ctl.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Origin": f"http://127.0.0.1:{self.port}"}
        if cookie:
            h["Cookie"] = cookie
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r, (json.loads(raw) if raw and r.getheader("Content-Type", "").startswith("application/json") else raw)

    def login(self):
        r, _ = self.req("POST", "/admin/api/login", {"password": "1234"})
        return r.getheader("Set-Cookie").split(";", 1)[0]

    def test_requires_login(self):
        r, _ = self.req("GET", "/admin/api/control/state")
        self.assertEqual(r.status, 401)
        r, _ = self.req("POST", "/admin/api/control/command", {"action": "pause"})
        self.assertEqual(r.status, 401)

    def test_command_roundtrip_and_public_view(self):
        ck = self.login()
        r, j = self.req("GET", "/admin/api/control/state", cookie=ck)
        self.assertEqual(r.status, 200)
        self.assertTrue(j["control_allowed"])
        self.assertEqual(len(j["configs"]["irrigation"]), 8)
        r, j = self.req("POST", "/admin/api/control/command",
                        {"action": "on", "system": "lighting", "zone": 3}, cookie=ck)
        self.assertEqual(r.status, 200, j)
        time.sleep(1.2)
        r, j = self.req("GET", "/api/public/control")
        light = [s for s in j["systems"] if s["id"] == "lighting"][0]
        self.assertTrue(light["zones"][2]["on"])
        self.assertNotIn("reason", light["zones"][2])          # 공개 응답은 켜짐/꺼짐만
        r, j = self.req("POST", "/admin/api/control/command",
                        {"action": "on", "system": "irrigation", "zone": 1}, cookie=ck)
        self.assertEqual(r.status, 400)                        # 관수는 시간 필수
        self.assertIn("분", j["message"])

    def test_config_save_and_report_csv(self):
        ck = self.login()
        r, j = self.req("POST", "/admin/api/control/config", {"system": "irrigation", "zone": 2, "config": {
            "name": "토마토", "schedules": [{"start": "07:30", "duration_min": 3, "days": [0, 2, 4]}]}}, cookie=ck)
        self.assertEqual(r.status, 200, j)
        r, j = self.req("POST", "/admin/api/control/config", {"system": "irrigation", "zone": 2, "config": {
            "schedules": [{"start": "7:3", "duration_min": 3, "days": [0]}]}}, cookie=ck)
        self.assertEqual(r.status, 400)
        r, j = self.req("GET", "/admin/api/control/state", cookie=ck)
        self.assertEqual(j["configs"]["irrigation"][1]["name"], "토마토")
        r, body = self.req("GET", "/admin/api/control/report.csv?days=7", cookie=ck)
        self.assertEqual(r.status, 200)
        self.assertIn("시스템", body.decode("utf-8-sig"))

    def test_pin_blocked_when_real_hardware(self):
        (Path(self.tmp.name) / "config.yaml").write_text(
            "control:\n  hardware: mock\n  channels:\n    irrigation.1: { driver: gpio, pin: 5 }\n", encoding="utf-8")
        ck = self.login()
        r, j = self.req("POST", "/admin/api/control/command", {"action": "pause"}, cookie=ck)
        self.assertEqual(r.status, 403)
        self.assertEqual(j["error"], "strong_password_required")


if __name__ == "__main__":
    unittest.main()
