"""텔레그램 봇: 명령(/status, /temp, /alerts, /help)에 응답.

실행 (systemd 사용자 서비스 smartfarm-bot):
    PYTHONPATH=src .venv/bin/python -B -m smartfarm.notify.bot

- getUpdates 롱폴링 방식이라 외부에서 들어오는 포트·웹훅이 필요 없다.
- .env 의 TELEGRAM_CHAT_ID 에 등록된 대화에만 응답한다. 등록되지 않은 사용자에게는
  그 사람의 chat id 만 알려준다(등록 안내용). 측정값·상태는 보내지 않는다.
- 토큰이 없거나 템플릿 값이면 30초마다 .env 를 다시 읽으며 기다린다.
- 봇이 꺼져 있던 동안 쌓인 오래된 명령(2분 이상)은 응답하지 않고 건너뛴다.
- 로그는 표준출력(journalctl --user -u smartfarm-bot)으로만 남긴다.
"""
from __future__ import annotations

import logging
import signal
import sys
import time

from dotenv import dotenv_values

from . import commands
from .telegram import TelegramError, api_call, read_settings, redact, send_message

ROOT = commands.ROOT
ENV_PATH = ROOT / ".env"
POLL_TIMEOUT = 50
MAX_AGE_SEC = 120
WAIT_UNCONFIGURED = 30

log = logging.getLogger("smartfarm.bot")
_running = True


def _stop(signum, frame):  # noqa: ARG001
    global _running
    _running = False


def handle_update(upd: dict, allowed: set[int], bot_username: str | None, now: float):
    """업데이트 1건 처리 → (chat_id, 답장) 또는 None. 네트워크 없이 테스트 가능."""
    msg = upd.get("message") or upd.get("edited_message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None
    cmd = commands.parse_command(msg.get("text"), bot_username)
    if cmd is None:
        return None
    if now - (msg.get("date") or 0) > MAX_AGE_SEC:
        log.info("오래된 명령 건너뜀: /%s", cmd)
        return None
    if chat_id not in allowed:
        log.warning("등록되지 않은 대화의 명령 거부: chat_id=%s /%s", chat_id, cmd)
        return chat_id, (
            "⛔ 등록되지 않은 사용자입니다.\n"
            f"이 대화의 chat id: {chat_id}\n"
            "관리자가 스마트팜 서버 .env 의 TELEGRAM_CHAT_ID 에 등록해야 사용할 수 있습니다."
        )
    log.info("명령 /%s (chat_id=%s)", cmd, chat_id)
    try:
        return chat_id, commands.reply_for(cmd)
    except Exception:  # noqa: BLE001
        log.exception("명령 처리 오류: /%s", cmd)
        return chat_id, "⚠ 처리 중 오류가 발생했습니다. 잠시 후 다시 시도하세요."


def _settings():
    return read_settings({k: v or "" for k, v in dotenv_values(ENV_PATH).items()})


def run() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    token, chat_ids = _settings()
    warned = False
    while _running and not (token and chat_ids):
        if not warned:
            log.warning("텔레그램 미설정(토큰 또는 chat id 없음) — .env 설정을 기다립니다. "
                        "설정: python -m smartfarm.notify.setup_bot")
            warned = True
        time.sleep(WAIT_UNCONFIGURED)
        token, chat_ids = _settings()
    if not _running:
        return 0

    me = None
    while _running and me is None:
        try:
            me = api_call(token, "getMe")
        except TelegramError as e:
            if e.status in (401, 404):
                log.error("봇 토큰이 올바르지 않습니다(%s). .env 를 확인하세요. 10분 후 재시도.", e)
                time.sleep(600)
                token, chat_ids = _settings()
            else:
                log.warning("getMe 실패: %s — 30초 후 재시도", e)
                time.sleep(30)
        except RuntimeError as e:
            log.warning("텔레그램 연결 실패: %s — 30초 후 재시도", e)
            time.sleep(30)
    if not _running:
        return 0
    username = me.get("username")
    log.info("봇 시작: @%s, 허용 대화 %d개", username, len(chat_ids))
    try:
        api_call(token, "setMyCommands",
                 {"commands": [{"command": c, "description": d} for c, d in commands.COMMANDS]})
    except RuntimeError as e:
        log.warning("명령 메뉴 등록 실패(동작에는 지장 없음): %s", e)

    allowed = set(chat_ids)
    offset = None
    backoff = 5
    while _running:
        try:
            params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message", "edited_message"]}
            if offset is not None:
                params["offset"] = offset
            updates = api_call(token, "getUpdates", params, timeout=(10, POLL_TIMEOUT + 15))
            backoff = 5
        except TelegramError as e:
            if e.status == 409:
                log.error("다른 프로세스가 같은 봇으로 수신 중입니다(409). 60초 후 재시도.")
                time.sleep(60)
            else:
                log.warning("getUpdates 실패: %s — %d초 후 재시도", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)
            continue
        except RuntimeError as e:
            log.warning("텔레그램 연결 끊김: %s — %d초 후 재시도", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        for upd in updates or []:
            offset = max(offset or 0, upd.get("update_id", 0) + 1)
            res = handle_update(upd, allowed, username, time.time())
            if res is None:
                continue
            try:
                send_message(token, res[0], res[1])
            except RuntimeError as e:
                log.error("응답 전송 실패: %s", e)
    log.info("봇 종료")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)-7s %(name)s: %(message)s")
    # requests/urllib3 디버그 로그에는 토큰 URL 이 찍힐 수 있으므로 올리지 않는다.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        return run()
    except Exception as e:  # noqa: BLE001
        log.error("봇 비정상 종료: %s", redact(repr(e)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
