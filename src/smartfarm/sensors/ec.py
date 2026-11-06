"""EC(전기전도도) 센서 (아날로그 → I2C ADC 경유).

calibration.k: 셀 상수, temp_coef: 온도보정계수(기준 25°C).
"""
from __future__ import annotations

import random

from .base import Sensor


class ECSensor(Sensor):
    type = "ec"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.channel = spec.get("channel", 0)
        self.calibration = spec.get("calibration", {"k": 1.0, "temp_coef": 0.02})

    def _compensate(self, ec_raw: float, temp_c: float = 25.0) -> float:
        coef = self.calibration.get("temp_coef", 0.02)
        return round(ec_raw / (1 + coef * (temp_c - 25.0)), 3)

    def read(self) -> float:
        # TODO: ADC 전압 → EC 변환 후 온도보정(_compensate) 적용
        mock_ec = random.uniform(1.0, 2.6)
        return self._compensate(mock_ec)
