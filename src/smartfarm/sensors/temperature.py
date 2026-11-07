"""온도 센서 (DS18B20, 1-Wire).

여러 개의 DS18B20 은 GPIO4(물리 7번) 하나의 1-Wire 버스를 공유하며,
각 센서의 고유 주소(28-xxxxxxxxxxxx)로 구분한다.

사전 준비(최초 1회, sudo 필요):
  sudo raspi-config nonint do_onewire 0   # 1-Wire 활성화 (GPIO4)
  sudo reboot
  ls /sys/bus/w1/devices/                  # 28-... 주소 확인 → config.yaml 에 입력

커널이 각 센서를 /sys/bus/w1/devices/<주소>/w1_slave 로 노출한다.
주소가 비었거나 장치가 없으면(미연결) 모의값을 반환해 서버가 멈추지 않게 한다.
"""
from __future__ import annotations

import random
from pathlib import Path

from ..core.logging import get_logger
from .base import Sensor

log = get_logger(__name__)
W1_DIR = Path("/sys/bus/w1/devices")


class TemperatureSensor(Sensor):
    type = "temperature"

    def __init__(self, spec: dict):
        super().__init__(spec)
        # address 예: "28-0000012345ab" (DS18B20 ROM 코드)
        self.address = spec.get("address") or ""
        self._warned = False

    def _device_file(self) -> Path | None:
        if not self.address or not str(self.address).startswith("28-"):
            return None
        f = W1_DIR / str(self.address) / "w1_slave"
        return f if f.exists() else None

    def read(self) -> float:
        f = self._device_file()
        if f is None:
            self.simulated = True
            if not self._warned:
                log.warning(
                    "DS18B20 %s(addr=%r) 미연결/미설정 → 모의값 사용",
                    self.name, self.address,
                )
                self._warned = True
            return round(random.uniform(18.0, 28.0), 2)

        text = f.read_text()
        lines = text.splitlines()
        # 1행 끝에 CRC 상태(YES/NO), 2행에 "t=밀리섭씨"
        if not lines or not lines[0].strip().endswith("YES"):
            raise RuntimeError(f"DS18B20 {self.name} CRC 체크 실패")
        idx = text.find("t=")
        if idx < 0:
            raise RuntimeError(f"DS18B20 {self.name} 온도값 파싱 실패")
        self.simulated = False
        return round(int(text[idx + 2:]) / 1000.0, 3)
