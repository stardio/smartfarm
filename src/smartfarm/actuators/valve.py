"""밸브(릴레이) 제어.

gpiozero 가 설치/동작하는 환경에서는 실제 GPIO 제어, 아니면 모의 동작으로 폴백.
active_high=False 인 active-low 릴레이를 지원.
"""
from __future__ import annotations

from ..core.logging import get_logger

log = get_logger(__name__)

try:
    from gpiozero import OutputDevice  # type: ignore
    _HAS_GPIO = True
except Exception:  # noqa: BLE001
    _HAS_GPIO = False
    log.warning("gpiozero 사용 불가 → 밸브는 모의 모드로 동작합니다.")


class Valve:
    def __init__(self, spec: dict):
        self.name: str = spec["name"]
        self.pin: int = spec["pin"]
        self.active_high: bool = spec.get("active_high", False)
        self.role: str = spec.get("role", "")
        self._device = None
        self._state = False
        if _HAS_GPIO:
            self._device = OutputDevice(
                self.pin, active_high=self.active_high, initial_value=False
            )

    def open(self) -> None:
        self._state = True
        if self._device:
            self._device.on()
        log.info("밸브 OPEN: %s (pin %s)", self.name, self.pin)

    def close(self) -> None:
        self._state = False
        if self._device:
            self._device.off()
        log.info("밸브 CLOSE: %s (pin %s)", self.name, self.pin)

    def close_safe(self) -> None:
        """종료 시 안전하게 닫기."""
        try:
            self.close()
        except Exception:  # noqa: BLE001
            log.exception("밸브 닫기 실패: %s", self.name)

    @property
    def is_open(self) -> bool:
        return self._state


def build_valves(cfg: dict) -> list[Valve]:
    return [Valve(spec) for spec in cfg.get("valves", [])]
