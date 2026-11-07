"""자동 경보 / DS18B20 판독 테스트 (네트워크·하드웨어 없음).

실행:  PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_alerts
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smartfarm.alerts import AlertManager
from smartfarm.sensors import temperature as temp_mod


class FakeSensor:
    def __init__(self, name, type_="temperature", simulated=False):
        self.name, self.type, self.simulated = name, type_, simulated


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


CFG = {
    "rules": {"temperature": {"max": 30.0}, "ph": {"min": 5.5, "max": 6.5}},
    "alerts": {"on_threshold_breach": True, "confirm_count": 3, "remind_minutes": 60,
               "heartbeat_hours": 0},
}


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clock = Clock()
        self.mgr = AlertManager(CFG, self.sent.append, clock=self.clock)
        self.t1 = FakeSensor("temp_1")

    def feed(self, value, sensors=None, step=10):
        self.clock.t += step
        sensors = sensors or [self.t1]
        return self.mgr.update(sensors, {} if value is None else {s.name: value for s in sensors})

    def test_breach_needs_confirmation(self):
        self.assertEqual(self.feed(31.0), [])
        self.assertEqual(self.feed(31.2), [])
        out = self.feed(31.4)
        self.assertEqual(len(out), 1)
        self.assertIn("[경보] temp_1 온도 31.4 °C — 최대 30.0 °C 초과", out[0])
        self.assertEqual(self.feed(31.5), [])          # 중복 경보 없음

    def test_single_spike_ignored(self):
        for v in (31.0, 29.0, 31.0, 29.0, 31.0, 29.0):
            self.assertEqual(self.feed(v), [])

    def test_recovery_with_hysteresis(self):
        for _ in range(3):
            self.feed(31.0)
        for _ in range(5):                              # 29.8 은 기준 안이지만 여유값(0.5) 안쪽 아님
            self.assertEqual(self.feed(29.8), [])
        self.feed(29.4)
        self.feed(29.3)
        out = self.feed(29.2)
        self.assertEqual(len(out), 1)
        self.assertIn("[복구] temp_1 온도 29.2 °C", out[0])
        self.assertIn("이탈 지속", out[0])

    def test_reminder(self):
        for _ in range(3):
            self.feed(31.0)
        self.assertEqual(self.feed(31.0, step=59 * 60), [])
        out = self.feed(31.0, step=61)
        self.assertEqual(len(out), 1)
        self.assertIn("[경보 지속]", out[0])

    def test_low_limit(self):
        ph = FakeSensor("ph_1", "ph")
        for _ in range(2):
            self.feed(5.0, [ph])
        out = self.feed(5.0, [ph])
        self.assertIn("pH 5.00 — 최소 5.50 미만", out[0])

    def test_sensor_failure_and_recovery(self):
        self.feed(None)
        self.feed(None)
        out = self.feed(None)
        self.assertIn("[센서 오류] temp_1", out[0])
        self.assertEqual(self.feed(None), [])
        self.feed(22.0)
        self.feed(22.0)
        out = self.feed(22.0)
        self.assertIn("[센서 복구] temp_1 온도 정상 측정 22.0 °C", out[0])

    def test_simulated_sensors_skipped(self):
        ph = FakeSensor("ph_1", "ph", simulated=True)
        for _ in range(5):
            self.assertEqual(self.feed(9.0, [ph]), [])

    def test_disabled(self):
        mgr = AlertManager(dict(CFG, alerts={"on_threshold_breach": False}), self.sent.append, clock=self.clock)
        for _ in range(5):
            self.clock.t += 10
            self.assertEqual(mgr.update([self.t1], {"temp_1": 40.0}), [])

    def test_heartbeat(self):
        cfg = dict(CFG, alerts=dict(CFG["alerts"], heartbeat_hours=12))
        mgr = AlertManager(cfg, self.sent.append, clock=self.clock, heartbeat_text=lambda: "요약")
        self.clock.t += 11 * 3600
        self.assertEqual(mgr.update([], {}), [])
        self.clock.t += 3601
        out = mgr.update([], {})
        self.assertEqual(out, ["🕒 정기 보고\n\n요약"])
        self.assertEqual(mgr.update([], {}), [])

    def test_send_error_does_not_break(self):
        def boom(_):
            raise RuntimeError("network")
        mgr = AlertManager(CFG, boom, clock=self.clock)
        for _ in range(3):
            self.clock.t += 10
            out = mgr.update([self.t1], {"temp_1": 35.0})
        self.assertEqual(len(out), 1)


class DS18B20Test(unittest.TestCase):
    def sensor(self, d, text=None):
        if text is not None:
            dev = Path(d) / "28-abc"
            dev.mkdir()
            (dev / "w1_slave").write_text(text)
        return temp_mod.TemperatureSensor({"name": "temp_1", "address": "28-abc"})

    def test_ok(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(temp_mod, "W1_DIR", Path(d)):
            s = self.sensor(d, "72 01 4b 46 7f ff 0e 10 57 : crc=57 YES\n72 01 4b 46 7f ff 0e 10 57 t=23125\n")
            self.assertEqual(s.read(), 23.125)
            self.assertFalse(s.simulated)

    def test_missing_configured_sensor_raises(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(temp_mod, "W1_DIR", Path(d)):
            s = self.sensor(d)
            with self.assertRaises(RuntimeError):
                s.read()
            self.assertFalse(s.simulated)

    def test_power_on_value_rejected(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(temp_mod, "W1_DIR", Path(d)):
            s = self.sensor(d, "50 05 4b 46 7f ff 0c 10 1c : crc=1c YES\n50 05 4b 46 7f ff 0c 10 1c t=85000\n")
            with self.assertRaises(RuntimeError):
                s.read()

    def test_crc_fail(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(temp_mod, "W1_DIR", Path(d)):
            s = self.sensor(d, "ff ff : crc=00 NO\nff ff t=-62\n")
            with self.assertRaises(RuntimeError):
                s.read()

    def test_unconfigured_is_mock(self):
        s = temp_mod.TemperatureSensor({"name": "temp_9", "address": ""})
        v = s.read()
        self.assertTrue(18.0 <= v <= 28.0)
        self.assertTrue(s.simulated)


if __name__ == "__main__":
    unittest.main()
