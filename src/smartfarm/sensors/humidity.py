"""습도 센서 (예: SHT31 I2C)."""
from __future__ import annotations

import random

from .base import Sensor


class HumiditySensor(Sensor):
    type = "humidity"

    def read(self) -> float:
        # TODO: 실제 센서 읽기로 교체
        return round(random.uniform(45.0, 75.0), 2)
