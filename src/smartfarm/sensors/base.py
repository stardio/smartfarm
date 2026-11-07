"""센서 공통 추상 클래스."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Sensor(ABC):
    type: str = "unknown"
    # 마지막 read() 값이 모의값이었는지 여부. 실제 드라이버는 실측 시 False 로 둔다.
    simulated: bool = True

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
