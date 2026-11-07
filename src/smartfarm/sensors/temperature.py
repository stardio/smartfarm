"""온도 센서 (DS18B20, 1-Wire).

여러 개의 DS18B20 은 GPIO4(물리 7번) 하나의 1-Wire 버스를 공유하며,
각 센서의 고유 주소(28-xxxxxxxxxxxx)로 구분한다.

사전 준비(최초 1회, sudo 필요):
  sudo raspi-config nonint do_onewire 0   # 1-Wire 활성화 (GPIO4)
  sudo reboot
  ls /sys/bus/w1/devices/                  # 28-... 주소 확인 → config.yaml 에 입력

커널이 각 센서를 /sys/bus/w1/devices/<주소>/w1_slave 로 노출한다.
주소가 비어 있으면(아직 미설치) 모의값을 반환한다. 주소가 설정된 센서가 사라지면
(선 빠짐·고장) 모의값으로 바꾸지 않고 오류를 내서 센서 오류 경보가 나가게 한다.
85.000 °C(전원 리셋 직후 기본값)와 측정 범위(-55~125 °C) 밖 값도 오류로 처리한다.
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

    def _configured(self) -> bool:
        return bool(self.address) and str(self.address).startswith("28-")

    def read(self) -> float:
        if self._configured():
            self.simulated = False
            f = W1_DIR / str(self.address) / "w1_slave"
            try:
                text = f.read_text()
            except OSError:
                raise RuntimeError(f"DS18B20 {self.name}({self.address}) 응답 없음 — 배선 확인") from None
            return self._parse(text)
        else:
            self.simulated = True
            if not self._warned:
                log.warning(
                    "DS18B20 %s(addr=%r) 미연결/미설정 → 모의값 사용",
                    self.name, self.address,
                )
                self._warned = True
            return round(random.uniform(18.0, 28.0), 2)

    def _parse(self, text: str) -> float:
        lines = text.splitlines()
        # 1행 끝에 CRC 상태(YES/NO), 2행에 "t=밀리섭씨"
        if not lines or not lines[0].strip().endswith("YES"):
            raise RuntimeError(f"DS18B20 {self.name} CRC 체크 실패")
        idx = text.find("t=")
        if idx < 0:
            raise RuntimeError(f"DS18B20 {self.name} 온도값 파싱 실패")
        milli = int(text[idx + 2:].split()[0])
        if milli == 85000 or not -55000 <= milli <= 125000:
            raise RuntimeError(f"DS18B20 {self.name} 비정상 값 {milli / 1000:.3f} °C")
        return round(milli / 1000.0, 3)
