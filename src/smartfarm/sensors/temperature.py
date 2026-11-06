"""온도 센서 (DS18B20 1-Wire 또는 I2C 온습도 모듈).

실제 하드웨어 연결 전에는 모의값을 반환합니다.
DS18B20 사용 시 w1thermsensor, I2C 모듈은 해당 라이브러리로 교체하세요.
"""
from __future__ import annotations

import random

from .base import Sensor


class TemperatureSensor(Sensor):
    type = "temperature"

    def read(self) -> float:
        # TODO: 실제 센서 읽기로 교체
        return round(random.uniform(18.0, 28.0), 2)
