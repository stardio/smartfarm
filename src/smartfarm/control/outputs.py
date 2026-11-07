"""출력 드라이버.

출력이 40개 이상(관수 8+펌프, 조명 8, 팬 8, 히터 8, 양액 6)이라 Pi GPIO 만으로는 부족하다.
하드웨어가 정해지면 여기에 드라이버를 추가한다(예: MCP23017 I2C 확장, RS485 Modbus 릴레이 보드).

config.yaml:
  control:
    hardware: mock                # 전체 기본 드라이버. mock = 실제 출력 없음(시뮬레이션)
    channels:                     # 채널별 지정(선택). 키 = "시스템.구역"
      irrigation.1: { driver: gpio, pin: 5, active_high: false }
"""
from __future__ import annotations

from ..core.logging import get_logger

log = get_logger(__name__)


class Output:
    driver = "base"

    def __init__(self, key: str):
        self.key = key
        self.state = False

    def set(self, on: bool) -> None:
        self.state = bool(on)

    def close(self) -> None:
        self.set(False)


class MockOutput(Output):
    """실제 출력 없음. 상태만 기억한다(개발·시험용)."""
    driver = "mock"


class GpioOutput(Output):
    """Pi GPIO 직접 연결 릴레이. gpiozero + lgpio 백엔드 필요."""
    driver = "gpio"

    def __init__(self, key: str, pin: int, active_high: bool = False):
        super().__init__(key)
        from gpiozero import DigitalOutputDevice      # 하드웨어 사용 시에만 import
        self.dev = DigitalOutputDevice(pin, active_high=active_high, initial_value=False)

    def set(self, on: bool) -> None:
        if on:
            self.dev.on()
        else:
            self.dev.off()
        self.state = bool(on)

    def close(self) -> None:
        try:
            self.dev.off()
            self.dev.close()
        finally:
            self.state = False


def build_output(key: str, control_cfg: dict) -> Output:
    spec = ((control_cfg or {}).get("channels") or {}).get(key) or {}
    driver = spec.get("driver") or (control_cfg or {}).get("hardware", "mock")
    if driver == "mock":
        return MockOutput(key)
    if driver == "gpio":
        try:
            return GpioOutput(key, int(spec["pin"]), bool(spec.get("active_high", False)))
        except Exception as e:  # noqa: BLE001
            log.error("출력 %s GPIO 초기화 실패(%s) → 안전을 위해 mock 으로 대체", key, e)
            return MockOutput(key)
    log.error("출력 %s: 알 수 없는 드라이버 %r → mock 으로 대체", key, driver)
    return MockOutput(key)
