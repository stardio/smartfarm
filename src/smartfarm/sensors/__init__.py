"""센서 팩토리: config 의 sensors 목록 → 센서 객체 리스트."""
from __future__ import annotations

from .base import Sensor
from .temperature import TemperatureSensor
from .humidity import HumiditySensor
from .ph import PHSensor
from .ec import ECSensor

_TYPES = {
    "temperature": TemperatureSensor,
    "humidity": HumiditySensor,
    "ph": PHSensor,
    "ec": ECSensor,
}


def build_sensors(cfg: dict) -> list[Sensor]:
    sensors: list[Sensor] = []
    for spec in cfg.get("sensors", []):
        cls = _TYPES.get(spec["type"])
        if cls is None:
            raise ValueError(f"알 수 없는 센서 type: {spec['type']}")
        sensors.append(cls(spec))
    return sensors
