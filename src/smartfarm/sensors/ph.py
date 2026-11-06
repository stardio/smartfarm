"""pH 센서 (아날로그 → ADS1115 등 I2C ADC 경유).

calibration: 2점 보정(ph4, ph7 전압)으로 전압→pH 변환.
"""
from __future__ import annotations

import random

from .base import Sensor


class PHSensor(Sensor):
    type = "ph"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.channel = spec.get("channel", 0)
        self.calibration = spec.get("calibration", {"ph4": 3.05, "ph7": 2.54})

    def _voltage_to_ph(self, voltage: float) -> float:
        v4, v7 = self.calibration["ph4"], self.calibration["ph7"]
        slope = (7.0 - 4.0) / (v7 - v4)
        return round(7.0 + (voltage - v7) * slope, 2)

    def read(self) -> float:
        # TODO: ADC 에서 전압 읽어 _voltage_to_ph 로 변환
        mock_voltage = random.uniform(2.4, 3.1)
        return self._voltage_to_ph(mock_voltage)
