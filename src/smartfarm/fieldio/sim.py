"""시뮬레이션 Modbus 장비 (개발·시험용). 실제 장비처럼 PDU 를 받아 응답한다."""
from __future__ import annotations

import random
import struct


class SimDevice:
    def __init__(self, kind: str = "relay", channels: int = 16):
        self.kind = kind
        self.online = True
        self.coils = [False] * 256
        self.holding = [0] * 256
        self.inputs = [0] * 256
        self.channels = channels
        self._t = 22.0 + random.uniform(-1.5, 1.5)
        self._h = 60.0 + random.uniform(-5, 5)
        self.writes = 0

    def _tick_sensor(self):
        self._t = min(35.0, max(5.0, self._t + random.uniform(-0.1, 0.1)))
        self._h = min(95.0, max(20.0, self._h + random.uniform(-0.3, 0.3)))
        t = int(round(self._t * 10)) & 0xFFFF
        h = int(round(self._h * 10))
        # 흔한 온습도 센서 배치: 입력 레지스터 1=온도×10, 2=습도×10 (홀딩 0,1 에도 같은 값)
        self.inputs[1], self.inputs[2] = t, h
        self.holding[0], self.holding[1] = t, h

    def handle(self, pdu: bytes) -> bytes:
        fn = pdu[0]
        if self.kind == "sensor":
            self._tick_sensor()
        try:
            if fn in (1, 2):
                addr, n = struct.unpack(">HH", pdu[1:5])
                bits = self.coils[addr:addr + n]
                if len(bits) < n:
                    return bytes([fn | 0x80, 2])
                data = bytearray((n + 7) // 8)
                for i, v in enumerate(bits):
                    if v:
                        data[i // 8] |= 1 << (i % 8)
                return bytes([fn, len(data)]) + bytes(data)
            if fn in (3, 4):
                addr, n = struct.unpack(">HH", pdu[1:5])
                regs = (self.holding if fn == 3 else self.inputs)[addr:addr + n]
                if len(regs) < n:
                    return bytes([fn | 0x80, 2])
                return bytes([fn, n * 2]) + struct.pack(f">{n}H", *regs)
            if fn == 5:
                addr, val = struct.unpack(">HH", pdu[1:5])
                if addr >= self.channels:
                    return bytes([fn | 0x80, 2])
                self.coils[addr] = val == 0xFF00
                self.writes += 1
                return pdu[:5]
            if fn == 6:
                addr, val = struct.unpack(">HH", pdu[1:5])
                self.holding[addr] = val
                # 레지스터 방식 릴레이 보드 흉내: 1..N 번지에 0x0100=켜기, 0x0200=끄기
                if self.kind == "relay" and 1 <= addr <= self.channels:
                    if val == 0x0100:
                        self.coils[addr - 1] = True
                    elif val == 0x0200:
                        self.coils[addr - 1] = False
                    self.holding[addr] = 1 if self.coils[addr - 1] else 0
                self.writes += 1
                return pdu[:5]
            if fn == 15:
                addr, n, _bc = struct.unpack(">HHB", pdu[1:6])
                data = pdu[6:]
                if addr + n > self.channels:
                    return bytes([fn | 0x80, 2])
                for i in range(n):
                    self.coils[addr + i] = bool(data[i // 8] >> (i % 8) & 1)
                self.writes += 1
                return pdu[:5]
        except (struct.error, IndexError):
            return bytes([fn | 0x80, 3])
        return bytes([fn | 0x80, 1])
