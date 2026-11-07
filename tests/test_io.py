"""Modbus 드라이버 · I/O 관리자 · I/O 웹 API 테스트 (실제 장비 없음).

실행:  PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_io
"""
from __future__ import annotations

import http.client
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from smartfarm.control.engine import Controller
from smartfarm.control.store import ControlStore
from smartfarm.fieldio.manager import IOManager
from smartfarm.fieldio.modbus import ModbusError, ModbusTimeout, RtuTransport, TcpTransport, crc16
from smartfarm.fieldio.sim import SimDevice
from smartfarm.fieldio.store import IOConfigError, IOStore
from smartfarm.web import admin
from smartfarm.web.public_server import make_server


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


# ── 프로토콜 ───────────────────────────────────────────────
class FakeTcpServer:
    """SimDevice 로 응답하는 최소 Modbus TCP 서버."""

    def __init__(self, devices):
        self.devices = devices
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        with c:
            while True:
                head = c.recv(7)
                if len(head) < 7:
                    return
                tid, _p, length, unit = struct.unpack(">HHHB", head)
                pdu = c.recv(length - 1)
                dev = self.devices.get(unit)
                if dev is None:
                    continue                           # 응답 없음 → 클라이언트 타임아웃
                resp = dev.handle(pdu)
                c.sendall(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)

    def close(self):
        self.sock.close()


class FakeSerial:
    def __init__(self, devices, corrupt=False):
        self.devices, self.corrupt, self.buf = devices, corrupt, b""

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, frame):
        assert crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]
        dev = self.devices.get(frame[0])
        if dev is None:
            return
        body = bytes([frame[0]]) + dev.handle(frame[1:-2])
        crc = crc16(body)
        if self.corrupt:
            crc ^= 0xFFFF
        self.buf = body + struct.pack("<H", crc)

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def close(self):
        pass


class ProtocolTest(unittest.TestCase):
    def test_crc(self):
        self.assertEqual(crc16(bytes.fromhex("010300000001")), 0x0A84)    # 표준 예: 01 03 00 00 00 01 84 0A

    def test_tcp(self):
        relay, sensor = SimDevice("relay", 8), SimDevice("sensor")
        srv = FakeTcpServer({1: relay, 2: sensor})
        t = TcpTransport("127.0.0.1", srv.port, timeout=0.3)
        try:
            t.write_coil(1, 3, True)
            self.assertEqual(t.read_bits(1, 1, 0, 8), [False, False, False, True, False, False, False, False])
            t.write_coils(1, 0, [True] * 8)
            self.assertTrue(all(t.read_bits(1, 1, 0, 8)))
            temp, hum = t.read_registers(2, 4, 1, 2)
            self.assertTrue(50 <= temp <= 350 and 200 <= hum <= 950)
            with self.assertRaises(ModbusError):
                t.write_coil(1, 50, True)                       # 범위 밖 → 예외 응답
            with self.assertRaises(ModbusTimeout):
                t.read_registers(9, 3, 0, 1)                    # 없는 국번
            t.write_coil(1, 0, False)                           # 타임아웃 후 재연결
            self.assertFalse(t.read_bits(1, 1, 0, 1)[0])
        finally:
            t.close()
            srv.close()

    def test_rtu(self):
        relay = SimDevice("relay", 8)
        t = RtuTransport("/dev/ttyFAKE", 9600, serial_factory=lambda: FakeSerial({5: relay}))
        t.write_register(5, 2, 0x0100)                          # 레지스터 방식 2번 릴레이 켜기
        self.assertTrue(relay.coils[1])
        self.assertEqual(t.read_registers(5, 3, 1, 3), [0, 1, 0])
        with self.assertRaises(ModbusTimeout):
            t.read_registers(6, 3, 0, 1)
        bad = RtuTransport("/dev/ttyFAKE", 9600, serial_factory=lambda: FakeSerial({5: relay}, corrupt=True))
        with self.assertRaisesRegex(ModbusTimeout, "CRC"):
            bad.read_registers(5, 3, 0, 1)


# ── I/O 관리자 ─────────────────────────────────────────────
class ManagerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "control.db"
        self.store = IOStore(self.db)
        self.sent = []
        self.clock = Clock()
        self.bus = self.store.save_bus({"name": "sim", "kind": "sim"})
        self.relay = self.store.save_device({"name": "릴레이1", "bus_id": self.bus, "unit": 1, "profile": "relay_coil",
                                             "options": {"channels": 8}})
        self.iom = IOManager(self.store, self.sent.append, Path(self.tmp.name) / "io.json", clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def sim(self, unit=1):
        return self.iom.sims[self.bus][unit]

    def poll(self, n=1, step=3.0):
        for _ in range(n):
            self.clock.t += step
            self.iom.poll_once()


class ManagerTest(ManagerBase):
    def test_map_write_and_reassert(self):
        self.store.set_map(self.relay, 2, "irrigation.1")
        self.iom.reload()
        self.sim().coils[5] = True                              # 할당 안 된 릴레이 — 처음 연결 시 꺼짐
        self.poll()
        self.assertTrue(self.iom.devices[self.relay].online)
        self.assertFalse(self.sim().coils[5])
        self.assertTrue(self.iom.set_channel("irrigation.1", True))
        self.assertTrue(self.sim().coils[2])
        self.assertFalse(self.iom.set_channel("lighting.1", True))      # 할당 없음 → 시뮬레이션
        self.sim().coils[2] = False                             # 보드 재부팅 흉내
        self.poll()
        self.assertTrue(self.sim().coils[2])                    # 명령 상태로 다시 씀

    def test_offline_alert_and_recovery(self):
        self.store.set_map(self.relay, 0, "fan.1")
        self.iom.reload()
        self.poll()
        self.iom.set_channel("fan.1", True)
        self.sim().online = False
        self.poll(3, step=6)
        self.assertFalse(self.iom.devices[self.relay].online)
        self.assertTrue(any("통신 끊김" in m for m in self.sent))
        with self.assertRaises(RuntimeError):
            self.iom.set_channel("fan.1", False)
        self.sim().online = True
        self.sim().coils[0] = True                              # 끊긴 동안 상태가 달라졌다고 가정
        self.poll(1, step=6)
        self.assertTrue(any("통신 복구" in m for m in self.sent))
        self.assertFalse(self.sim().coils[0])                   # 마지막 명령(끄기) 상태로 맞춤

    def test_unmap_turns_relay_off_and_move(self):
        self.store.set_map(self.relay, 1, "heater.2")
        self.iom.reload()
        self.poll()
        self.iom.set_channel("heater.2", True)
        self.assertTrue(self.sim().coils[1])
        self.store.set_map(self.relay, 4, "heater.2")           # 다른 릴레이로 옮김
        self.iom.reload()
        self.assertFalse(self.sim().coils[1])
        self.assertEqual(self.iom.map["heater.2"], (self.relay, 4))

    def test_register_profile(self):
        d = self.store.save_device({"name": "레지스터보드", "bus_id": self.bus, "unit": 2, "profile": "relay_register"})
        self.store.set_map(d, 0, "lighting.3")
        self.iom.reload()
        self.poll()
        self.iom.set_channel("lighting.3", True)
        self.assertTrue(self.sim(2).coils[0])
        self.assertEqual(self.sim(2).holding[1], 1)

    def test_modbus_sensor(self):
        d = self.store.save_device({"name": "온습도1", "bus_id": self.bus, "unit": 10, "profile": "th_sensor"})
        self.store.save_sensor("temp_z1", d, {"type": "temperature", "fn": 4, "addr": 1, "scale": 0.1, "signed": True})
        self.iom.reload()
        self.poll()
        sensors = self.iom.sensors()
        self.assertEqual([s.name for s in sensors], ["temp_z1"])
        self.assertTrue(sensors[0].simulated)                   # 시뮬레이터 버스 → 모의값
        v = sensors[0].read()
        self.assertTrue(5 <= v <= 35)
        self.assertIn("temperature", self.iom.devices[d].raw_points)
        self.clock.t += 60                                      # 값이 오래되면 오류
        with self.assertRaises(RuntimeError):
            sensors[0].read()

    def test_pulse_and_scan_jobs(self):
        self.iom.reload()
        self.poll()
        res = self.iom.run_job({"action": "io_pulse", "device_id": self.relay, "idx": 6, "seconds": 2})
        self.assertIn("2초", res["message"])
        self.assertTrue(self.sim().coils[6])
        self.poll(1, step=2.5)
        self.assertFalse(self.sim().coils[6])
        res = self.iom.run_job({"action": "io_scan", "bus_id": self.bus, "start": 1, "end": 4})
        self.assertEqual([f["unit"] for f in res["found"]], [1])
        with self.assertRaises(ValueError):
            self.iom.run_job({"action": "io_pulse", "device_id": self.relay, "idx": 6, "seconds": 60})

    def test_validation(self):
        with self.assertRaises(IOConfigError):
            self.store.save_device({"name": "중복", "bus_id": self.bus, "unit": 1, "profile": "relay_coil"})
        with self.assertRaises(IOConfigError):
            self.store.save_bus({"name": "x", "kind": "tcp", "host": "bad host!"})
        with self.assertRaises(IOConfigError):
            self.store.save_bus({"name": "x", "kind": "rtu", "port": "/etc/passwd"})
        with self.assertRaises(IOConfigError):
            self.store.set_map(self.relay, 8, "fan.1")          # 8채널 → 0~7
        with self.assertRaises(IOConfigError):
            self.store.set_map(self.relay, 0, "nope.1")
        with self.assertRaises(IOConfigError):
            self.store.save_sensor("Temp!", self.relay, {"type": "temperature", "fn": 3, "addr": 0})
        self.assertFalse(self.store.has_real_devices())
        b = self.store.save_bus({"name": "gw", "kind": "tcp", "host": "192.168.10.20", "port": 502})
        self.store.save_device({"name": "실제", "bus_id": b, "unit": 1, "profile": "relay_coil"})
        self.assertTrue(self.store.has_real_devices())


class ControllerIOTest(ManagerBase):
    def test_controller_drives_mapped_relay_and_pulse_needs_pause(self):
        self.store.set_map(self.relay, 0, "lighting.1")
        self.iom.reload()
        self.poll()
        cstore = ControlStore(self.db)
        ctl = Controller({"control": {}}, cstore, clock=self.clock, status_path=Path(self.tmp.name) / "c.json", iom=self.iom)
        ctl.execute({"action": "on", "system": "lighting", "zone": 1})
        ctl.tick()
        self.assertTrue(self.sim().coils[0])
        snap = ctl.snapshot()
        z = snap["systems"][2]["zones"][0]
        self.assertEqual(z["io"]["label"], "릴레이1 #1")
        self.assertEqual(snap["hardware"], "modbus-sim")
        cid = cstore.add_command({"action": "io_pulse", "device_id": self.relay, "idx": 3, "seconds": 2})
        ctl.tick()
        self.assertEqual(cstore.command_result(cid)["status"], "error")    # 자동 운전 중 → 거부
        ctl.execute({"action": "pause"})
        self.assertFalse(self.sim().coils[0])                   # 전체 정지 → 릴레이 OFF
        cid = cstore.add_command({"action": "io_pulse", "device_id": self.relay, "idx": 3, "seconds": 2})
        ctl.tick()
        self.assertEqual(cstore.command_result(cid)["status"], "running")
        cid2, cmd = self.iom.jobs.get_nowait()
        self.iom.run_job(cmd)
        self.assertTrue(self.sim().coils[3])

    def test_comm_failure_keeps_retrying(self):
        self.store.set_map(self.relay, 0, "lighting.1")
        self.iom.reload()
        self.poll()
        ctl = Controller({"control": {}}, ControlStore(self.db), clock=self.clock,
                         status_path=Path(self.tmp.name) / "c.json", iom=self.iom)
        self.sim().online = False
        self.poll(3, step=6)
        ctl.execute({"action": "on", "system": "lighting", "zone": 1})
        ctl.tick()
        ch = ctl.channels[("lighting", 1)]
        self.assertFalse(ch.on)
        self.assertIn("통신 끊김", ch.out_error)
        self.sim().online = True
        self.poll(1, step=6)                                    # 재연결 → 명령 상태(켜기)로 맞춤
        self.assertTrue(self.sim().coils[0])
        ctl.tick()
        self.assertTrue(ch.on)
        self.assertEqual(ch.out_error, "")


# ── 웹 ─────────────────────────────────────────────────────
class IOWebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "config.yaml").write_text("sensors:\n  - { name: temp_1, type: temperature }\n", encoding="utf-8")
        (d / "io.json").write_text(json.dumps({"updated_at": time.time(), "buses": [], "devices": []}), encoding="utf-8")
        self.httpd = make_server("127.0.0.1", 0, d / "x.db", d / "s.json", admin_hash=admin.hash_password("1234"),
                                 config_path=d / "config.yaml", log_path=d / "x.log", admin_kind="pin",
                                 control_db=d / "control.db", control_status=d / "c.json", io_status=d / "io.json")
        self.httpd.RequestHandlerClass.auth.FAIL_DELAY_SEC = 0
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Origin": f"http://127.0.0.1:{self.port}"}
        if cookie:
            h["Cookie"] = cookie
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        j = json.loads(r.read() or b"null")
        c.close()
        return r, j

    def test_flow_and_pin_lock(self):
        r, _ = self.req("GET", "/admin/api/io/state")
        self.assertEqual(r.status, 401)
        r, _ = self.req("POST", "/admin/api/login", {"password": "1234"})
        ck = r.getheader("Set-Cookie").split(";", 1)[0]
        r, j = self.req("POST", "/admin/api/io", {"op": "save_bus", "bus": {"name": "연습", "kind": "sim"}}, ck)
        self.assertEqual(r.status, 200, j)
        r, j = self.req("POST", "/admin/api/io", {"op": "save_device", "device": {
            "name": "릴레이", "bus_id": j["id"], "unit": 1, "profile": "relay_coil"}}, ck)
        self.assertEqual(r.status, 200, j)
        r, j = self.req("POST", "/admin/api/io", {"op": "set_map", "device_id": j["id"], "idx": 0, "channel": "irrigation.pump"}, ck)
        self.assertEqual(r.status, 200, j)
        r, j = self.req("GET", "/admin/api/io/state", cookie=ck)
        self.assertEqual(j["config"]["map"]["irrigation.pump"]["idx"], 0)
        self.assertFalse(j["real"])
        r, j = self.req("POST", "/admin/api/io", {"op": "save_bus", "bus": {"name": "현장", "kind": "tcp", "host": "10.0.0.5"}}, ck)
        r, j = self.req("POST", "/admin/api/io", {"op": "save_device", "device": {
            "name": "현장보드", "bus_id": j["id"], "unit": 1, "profile": "relay_coil"}}, ck)
        self.assertEqual(r.status, 200)
        r, j = self.req("POST", "/admin/api/io", {"op": "delete_bus", "id": 1}, ck)
        self.assertEqual(r.status, 403)                          # 실제 장치 등록 후 PIN 으로는 변경 불가
        self.assertEqual(j["error"], "strong_password_required")


if __name__ == "__main__":
    unittest.main()
