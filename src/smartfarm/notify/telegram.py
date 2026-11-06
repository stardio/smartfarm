"""텔레그램 메시지 전송 (Bot HTTP API).

.env 의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 사용.
토큰이 없으면 전송하지 않고 로그만 남깁니다(개발 편의).
"""
from __future__ import annotations

import os

import requests

from ..core.logging import get_logger

log = get_logger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            log.warning("텔레그램 미설정(.env 확인) → 메시지는 로그로만 출력됩니다.")

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("[텔레그램-모의] %s", text)
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = requests.post(
                url, data={"chat_id": self.chat_id, "text": text}, timeout=10
            )
            r.raise_for_status()
            return True
        except Exception:  # noqa: BLE001
            log.exception("텔레그램 전송 실패")
            return False
