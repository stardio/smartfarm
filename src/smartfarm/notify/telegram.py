"""텔레그램 메시지 전송 (Bot HTTP API).

.env 의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 사용. TELEGRAM_CHAT_ID 는 쉼표로
여러 개 지정할 수 있다(모두에게 알림 전송, 모두 봇 명령 사용 가능).
토큰이 없거나 템플릿 값이면 전송하지 않고 로그만 남긴다(개발 편의).

토큰이 로그에 남지 않도록 오류 메시지는 항상 redact() 를 거친다
(requests 예외 메시지에는 토큰이 들어간 URL 이 포함된다).
"""
from __future__ import annotations

import os
import queue
import re
import threading

import requests

from ..core.envfile import is_placeholder
from ..core.logging import get_logger

log = get_logger(__name__)
API = "https://api.telegram.org"
MAX_TEXT = 4000          # 텔레그램 한도 4096자

_REDACT = [
    (re.compile(r"(/bot)[^/\s]+"), r"\1<redacted>"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "<redacted>"),
]


def redact(text: str) -> str:
    for pat, rep in _REDACT:
        text = pat.sub(rep, text)
    return text


def parse_chat_ids(raw: str | None) -> list[int]:
    if is_placeholder(raw):
        return []
    ids = []
    for part in (raw or "").split(","):
        part = part.strip()
        if re.fullmatch(r"-?\d{1,20}", part):
            ids.append(int(part))
        elif part:
            log.warning("TELEGRAM_CHAT_ID 형식 오류(숫자가 아님) — 무시: %s", part[:3] + "…")
    return ids


def read_settings(environ=None) -> tuple[str | None, list[int]]:
    """(token, chat_ids). 미설정·템플릿 값이면 token=None."""
    environ = os.environ if environ is None else environ
    token = (environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    return (None if is_placeholder(token) else token), parse_chat_ids(environ.get("TELEGRAM_CHAT_ID"))


def api_call(token: str, method: str, data: dict | None = None, timeout=15) -> dict:
    """Bot API 호출. 실패하면 토큰이 가려진 메시지로 RuntimeError."""
    try:
        r = requests.post(f"{API}/bot{token}/{method}", json=data or {}, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(redact(f"{type(e).__name__}: {e}")) from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not body.get("ok"):
        desc = body.get("description") or r.reason
        raise TelegramError(r.status_code, redact(str(desc)))
    return body.get("result")


class TelegramError(RuntimeError):
    def __init__(self, status: int, description: str):
        super().__init__(f"HTTP {status}: {description}")
        self.status = status


def send_message(token: str, chat_id: int, text: str) -> None:
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    api_call(token, "sendMessage", {"chat_id": chat_id, "text": text,
                                    "disable_web_page_preview": True})


class TelegramNotifier:
    """알림 전송. send() 는 대기열에 넣고 바로 돌아온다(측정 루프가 네트워크 때문에 멈추지 않게).

    전송은 백그라운드 스레드가 하며, 인터넷이 끊겨 있으면 10초→30초→1분→2분→5분 간격으로
    재시도한다(약 9분). 그래도 실패하면 버리고 로그에 남긴다.
    """
    RETRY_DELAYS = (10, 30, 60, 120, 300)
    QUEUE_MAX = 100

    def __init__(self):
        self.token, self.chat_ids = read_settings()
        self.enabled = bool(self.token and self.chat_ids)
        self._q: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX)
        self._stop = threading.Event()
        self._worker = None
        if not self.enabled:
            log.warning("텔레그램 미설정(.env 확인) → 메시지는 로그로만 출력됩니다.")
        else:
            self._worker = threading.Thread(target=self._run, name="telegram-sender", daemon=True)
            self._worker.start()

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("[텔레그램-모의] %s", text)
            return False
        try:
            self._q.put_nowait(text)
            return True
        except queue.Full:
            log.error("텔레그램 대기열 가득 참 — 메시지 버림: %s", text.splitlines()[0])
            return False

    def _deliver(self, cid: int, text: str) -> None:
        for i, delay in enumerate((0,) + self.RETRY_DELAYS):
            if delay and self._stop.wait(delay):
                break
            try:
                send_message(self.token, cid, text)
                return
            except TelegramError as e:
                if 400 <= e.status < 500 and e.status != 429:     # 잘못된 토큰·chat id 등: 재시도 무의미
                    log.error("텔레그램 전송 실패(재시도 안 함): %s", e)
                    return
                log.warning("텔레그램 전송 실패(%d회): %s", i + 1, e)
            except RuntimeError as e:
                log.warning("텔레그램 전송 실패(%d회): %s", i + 1, e)
        log.error("텔레그램 전송 포기: %s", text.splitlines()[0])

    def _run(self) -> None:
        while not (self._stop.is_set() and self._q.empty()):
            try:
                text = self._q.get(timeout=1)
            except queue.Empty:
                continue
            for cid in self.chat_ids:
                self._deliver(cid, text)

    def close(self, timeout: float = 10.0) -> None:
        """종료 시 대기 중인 메시지를 최대 timeout 초 동안 보내고 끝낸다."""
        if self._worker is None:
            return
        self._stop.set()
        self._worker.join(timeout)
