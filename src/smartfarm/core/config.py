"""설정 로더: config/config.yaml + .env 를 읽어 하나의 dict 로 제공."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# 프로젝트 루트 = 이 파일 기준 ../../../
ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "config.yaml"


def load_config() -> dict:
    load_dotenv(ROOT / ".env")  # .env → 환경변수

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"설정 파일이 없습니다: {CONFIG_PATH}\n"
            f"  cp config/config.example.yaml config/config.yaml  로 만든 뒤 값을 채우세요."
        )
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg.setdefault("alerts", {})
    return cfg


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)
