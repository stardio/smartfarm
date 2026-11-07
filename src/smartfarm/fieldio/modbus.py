"""Modbus 클라이언트 (외부 라이브러리 없이 직접 구현).

지원 기능 코드
  01 코일 읽기 · 02 입력 접점 읽기 · 03 홀딩 레지스터 읽기 · 04 입력 레지스터 읽기
  05 코일 1개 쓰기 · 06 레지스터 1개 쓰기 · 15(0x0F) 코일 여러 개 쓰기

전송 방식
  TcpTransport : Modbus TCP (MBAP 헤더). 게이트웨이·이더넷 릴레이 보드
  RtuTransport : Modbus RTU (CRC16) over 시리얼. USB-RS485 변환기 (pyserial 필요)
  SimTransport : 시뮬레이터 (실제 장비 없이 개발·시험)

한 버스(전송로)는 동시에 한 요청만 처리한다(lock). 버스마다 객체 하나.
"""
from __future__ import annotations

import socket
import struct
import threading
import time


class ModbusError(Exception):
    """장비가 예외 응답을 보냄 (잘못된 주소·기능 등)."""

    NAMES = {1: "지원하지 않는 기능", 2: "잘못된 주소", 3: "잘못된 값", 4: "장비 내부 오류", 6: "장비 사용 중"}

    def __init__(self, code: int):
        super().__init__(f"Modbus 예외 {code} ({self.NAMES.get(code, '알 수 없음')})")
        self.code = code


class ModbusTimeout(Exception):
    """응답 없음 / 연결 실패 / 응답 형식 오류."""


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


# ── PDU 만들기/해석 ────────────────────────────────────────
def pdu_read(fn: int, addr: int, count: int) -> bytes:
    return struct.pack(">BHH", fn, addr, count)


def pdu_write_coil(addr: int, on: bool) -> bytes:
    return struct.pack(">BHH", 5, addr, 0xFF00 if on else 0x0000)


def pdu_write_register(addr: int, value: int) -> bytes:
    return struct.pack(">BHH", 6, addr, value & 0xFFFF)


def pdu_write_coils(addr: int, values: list[bool]) -> bytes:
    n = len(values)
    data = bytearray((n + 7) // 8)
    for i, v in enumerate(values):
        if v:
            data[i // 8] |= 1 << (i % 8)
    return struct.pack(">BHHB", 15, addr, n, len(data)) + bytes(data)


def check_response(req: bytes, resp: bytes) -> bytes:
    if not resp:
        raise ModbusTimeout("빈 응답")
    if resp[0] == req[0] | 0x80:
        raise ModbusError(resp[1] if len(resp) > 1 else 0)
    if resp[0] != req[0]:
        raise ModbusTimeout(f"응답 기능 코드 불일치 ({resp[0]} ≠ {req[0]})")
    return resp


def parse_bits(resp: bytes, count: int) -> list[bool]:
    data = resp[2:2 + resp[1]]
    return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


def parse_registers(resp: bytes, count: int) -> list[int]:
    n = resp[1] // 2
    if n < count:
        raise ModbusTimeout("응답 레지스터 수 부족")
    return list(struct.unpack(f">{n}H", resp[2:2 + n * 2]))[:count]


# ── 전송로 ─────────────────────────────────────────────────
class Transport:
    kind = "base"

    def __init__(self, timeout: float = 0.5):
        self.timeout = timeout
        self.lock = threading.Lock()

    def transact(self, unit: int, pdu: bytes) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # 공통 명령
    def read_bits(self, unit, fn, addr, count) -> list[bool]:
        req = pdu_read(fn, addr, count)
        return parse_bits(check_response(req, self.transact(unit, req)), count)

    def read_registers(self, unit, fn, addr, count) -> list[int]:
        req = pdu_read(fn, addr, count)
        return parse_registers(check_response(req, self.transact(unit, req)), count)

    def write_coil(self, unit, addr, on) -> None:
        req = pdu_write_coil(addr, on)
        check_response(req, self.transact(unit, req))

    def write_register(self, unit, addr, value) -> None:
        req = pdu_write_register(addr, value)
        check_response(req, self.transact(unit, req))

    def write_coils(self, unit, addr, values) -> None:
        req = pdu_write_coils(addr, values)
        check_response(req, self.transact(unit, req))


class TcpTransport(Transport):
    kind = "tcp"

    def __init__(self, host: str, port: int = 502, timeout: float = 0.5):
        super().__init__(timeout)
        self.host, self.port = host, port
        self.sock = None
        self.tid = 0

    def _connect(self):
        if self.sock is None:
            try:
                self.sock = socket.create_connection((self.host, self.port), timeout=max(self.timeout, 1.0))
                self.sock.settimeout(self.timeout)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as e:
                self.sock = None
                raise ModbusTimeout(f"{self.host}:{self.port} 연결 실패 ({e.__class__.__name__})") from None

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ModbusTimeout("연결 끊김")
            buf += chunk
        return buf

    def transact(self, unit, pdu):
        with self.lock:
            self._connect()
            self.tid = (self.tid + 1) & 0xFFFF
            frame = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, unit) + pdu
            try:
                self.sock.sendall(frame)
                while True:
                    head = self._recv(7)
                    tid, proto, length, _unit = struct.unpack(">HHHB", head)
                    body = self._recv(length - 1)
                    if tid == self.tid and proto == 0:
                        return body
                    # 이전 요청의 늦은 응답은 버린다
            except (OSError, ModbusTimeout) as e:
                self.close()
                raise ModbusTimeout(f"응답 없음 ({e.__class__.__name__})") from None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


class RtuTransport(Transport):
    kind = "rtu"

    def __init__(self, port: str, baudrate: int = 9600, parity: str = "N", stopbits: int = 1,
                 timeout: float = 0.5, serial_factory=None):
        super().__init__(timeout)
        self.port, self.baudrate, self.parity, self.stopbits = port, baudrate, parity, stopbits
        self.serial_factory = serial_factory
        self.ser = None
        # 프레임 사이 최소 3.5 문자 시간 (19200bps 초과는 1.75ms 고정)
        self.gap = 0.00175 if baudrate > 19200 else 3.5 * 11 / baudrate
        self._last = 0.0

    def _open(self):
        if self.ser is None:
            try:
                if self.serial_factory:
                    self.ser = self.serial_factory()
                else:
                    import serial            # pyserial — RTU 사용할 때만 필요
                    self.ser = serial.Serial(self.port, self.baudrate, bytesize=8, parity=self.parity,
                                             stopbits=self.stopbits, timeout=self.timeout)
            except ImportError:
                raise ModbusTimeout("pyserial 미설치 (pip install pyserial)") from None
            except Exception as e:  # noqa: BLE001  (SerialException 등)
                self.ser = None
                raise ModbusTimeout(f"{self.port} 열기 실패 ({e.__class__.__name__})") from None

    def _read(self, n):
        data = self.ser.read(n)
        if len(data) < n:
            raise ModbusTimeout("응답 없음")
        return data

    def transact(self, unit, pdu):
        with self.lock:
            self._open()
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            frame = bytes([unit]) + pdu
            frame += struct.pack("<H", crc16(frame))
            try:
                self.ser.reset_input_buffer()
                self.ser.write(frame)
                head = self._read(2)
                if head[0] != unit:
                    raise ModbusTimeout("응답 국번 불일치")
                fn = head[1]
                if fn & 0x80:
                    rest = self._read(3)                      # 예외코드 + CRC
                elif fn in (1, 2, 3, 4):
                    cnt = self._read(1)
                    rest = cnt + self._read(cnt[0] + 2)
                else:                                         # 05 06 0F 10: 주소+값 4바이트 + CRC
                    rest = self._read(6)
                msg = head + rest
                if crc16(msg[:-2]) != struct.unpack("<H", msg[-2:])[0]:
                    raise ModbusTimeout("CRC 오류 (배선·종단저항·노이즈 확인)")
                return msg[1:-2]
            except ModbusTimeout:
                raise
            except Exception as e:  # noqa: BLE001
                self.close()
                raise ModbusTimeout(f"시리얼 오류 ({e.__class__.__name__})") from None
            finally:
                self._last = time.monotonic()

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.ser = None


class SimTransport(Transport):
    """시뮬레이터 버스: 국번별 SimDevice 가 응답한다."""
    kind = "sim"

    def __init__(self, devices: dict | None = None, timeout: float = 0.5):
        super().__init__(timeout)
        self.devices = devices if devices is not None else {}

    def transact(self, unit, pdu):
        with self.lock:
            dev = self.devices.get(unit)
            if dev is None or not dev.online:
                raise ModbusTimeout("응답 없음")
            return dev.handle(pdu)
