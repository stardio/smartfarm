"""엔트리포인트: 측정 → 저장 → 판단 → 제어 → 알림 루프.

실행:
    source .venv/bin/activate
    python -m smartfarm.main
"""
from __future__ import annotations

import signal
import time

from .core.config import load_config
from .core.logging import get_logger
from .sensors import build_sensors
from .actuators.valve import build_valves
from .notify.telegram import TelegramNotifier
from .storage.db import Database
from .status import write_status

log = get_logger(__name__)
_running = True


def _handle_stop(signum, frame):  # noqa: ARG001
    global _running
    log.info("종료 신호 수신(%s). 정리 후 종료합니다.", signum)
    _running = False


def main() -> None:
    cfg = load_config()
    db = Database(cfg)
    sensors = build_sensors(cfg)
    valves = build_valves(cfg)
    notifier = TelegramNotifier()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    if cfg["alerts"].get("send_startup_message"):
        notifier.send("🌱 스마트팜 서버 시작됨")

    log.info("센서 %d개, 밸브 %d개 로드 완료", len(sensors), len(valves))
    # 측정 주기(웹 표시·판단)와 DB 저장 주기를 분리한다.
    interval = cfg.get("sampling_interval_sec", 30)
    save_interval = cfg.get("db_save_interval_sec", interval)
    log.info("측정 주기 %s초, DB 저장 주기 %s초", interval, save_interval)
    last_save = None

    try:
        while _running:
            started = time.monotonic()
            # 측정 소요시간 때문에 생기는 오차를 흡수(반 주기 여유)
            save_now = last_save is None or started - last_save >= save_interval - interval / 2
            readings = {}
            for s in sensors:
                try:
                    value = s.read()
                    readings[s.name] = value
                    if save_now:
                        db.save_reading(s.name, s.type, value)
                    log.debug("%s(%s) = %s", s.name, s.type, value)
                except Exception:  # noqa: BLE001
                    log.exception("센서 읽기 실패: %s", s.name)
            if save_now:
                last_save = started

            # TODO: cfg["rules"] 기반 임계값 판단 → valve 제어 → 이탈 시 notifier.send(...)
            evaluate_rules(cfg, readings, valves, notifier)
            try:
                write_status(cfg, sensors, readings, valves)
            except Exception:  # noqa: BLE001
                log.exception("상태 파일 기록 실패")

            # 측정에 걸린 시간을 빼고 대기해 주기를 일정하게 유지
            time.sleep(max(0.0, interval - (time.monotonic() - started)))
    finally:
        for v in valves:
            v.close_safe()
        db.close()
        log.info("정상 종료")


def evaluate_rules(cfg, readings, valves, notifier) -> None:
    """임계값 판단 및 자동 제어 (구현 예정).

    예) pH/EC가 범위를 벗어나면 해당 밸브를 열고 텔레그램 경보 전송.
    """
    # TODO: 실제 제어 로직 구현
    return


if __name__ == "__main__":
    main()
