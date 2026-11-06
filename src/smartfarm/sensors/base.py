"""센서 공통 추상 클래스."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Sensor(ABC):
    type: str = "unknown"

    def __init__(self, spec: dict):
        self.spec = spec
        self.name: str = spec["name"]
        self.interface: str = spec.get("interface", "")
        self.address = spec.get("address")

    @abstractmethod
    def read(self) -> float:
        """측정값 1개를 반환. 하드웨어 연결 전에는 모의값을 반환하도록 구현."""
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"
