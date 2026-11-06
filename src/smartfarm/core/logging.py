"""로깅 설정: 콘솔 + 파일(logs/smartfarm.log)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    fileh = RotatingFileHandler(
        LOG_DIR / "smartfarm.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fileh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(fileh)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(name)
