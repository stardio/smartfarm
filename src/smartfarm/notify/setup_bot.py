"""텔레그램 봇 토큰·chat id 설정 도우미 (대화형).

    cd ~/smartfarm && PYTHONPATH=src .venv/bin/python -m smartfarm.notify.setup_bot

1) BotFather 에서 받은 토큰을 붙여넣는다(화면에 표시되지 않음) → getMe 로 확인
2) 휴대폰 텔레그램에서 그 봇에게 아무 메시지나 보낸다 → chat id 자동 확인
3) .env 의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 저장(권한 600) → 서비스 재시작
"""
from __future__ import annotations

import getpass
import re
import subprocess
import sys
import time

from ..core.envfile import update_env
from .commands import ROOT
from .telegram import api_call, send_message

ENV_PATH = ROOT / ".env"
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def wait_for_chat(token: str, timeout_sec: int = 180):
    """봇에게 온 첫 메시지의 (chat_id, 이름). 시간 초과면 None."""
    # 이전에 쌓인 메시지는 버리고 지금부터 온 메시지만 본다
    old = api_call(token, "getUpdates", {"timeout": 0})
    offset = (old[-1]["update_id"] + 1) if old else None
    end = time.time() + timeout_sec
    while time.time() < end:
        params = {"timeout": 20, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for upd in api_call(token, "getUpdates", params, timeout=(10, 40)) or []:
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or {}).get("chat") or {}
            if isinstance(chat.get("id"), int):
                name = chat.get("title") or " ".join(
                    x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
                # 확인한 메시지를 서버에서 지워 봇 서비스가 다시 처리하지 않게 한다
                api_call(token, "getUpdates", {"offset": offset, "timeout": 0})
                return chat["id"], name
    return None


def main() -> int:
    print("스마트팜 텔레그램 봇 설정")
    print("  봇 서비스가 실행 중이면 메시지를 가로채므로 먼저 멈춥니다.")
    subprocess.run(["systemctl", "--user", "stop", "smartfarm-bot"], check=False)

    token = getpass.getpass("BotFather 봇 토큰 붙여넣기 (화면에 표시되지 않음): ").strip()
    if not TOKEN_RE.fullmatch(token):
        print("토큰 형식이 아닙니다 (예: 123456789:AA... 형태).", file=sys.stderr)
        return 1
    try:
        me = api_call(token, "getMe")
    except RuntimeError as e:
        print(f"토큰 확인 실패: {e}", file=sys.stderr)
        return 1
    print(f"✔ 봇 확인: @{me.get('username')} ({me.get('first_name')})")
    print(f"\n휴대폰 텔레그램에서 @{me.get('username')} 를 열고 /start 또는 아무 메시지나 보내세요. (3분 대기)")
    try:
        found = wait_for_chat(token)
    except RuntimeError as e:
        print(f"메시지 수신 실패: {e}", file=sys.stderr)
        return 1
    if not found:
        print("3분 동안 메시지가 오지 않았습니다. 다시 실행하세요.", file=sys.stderr)
        return 1
    chat_id, name = found
    ans = input(f"✔ 메시지 확인: {name} (chat id {chat_id}). 이 대화를 등록할까요? [Y/n] ").strip().lower()
    if ans not in ("", "y", "yes", "ㅛ"):
        print("취소했습니다.")
        return 1

    update_env(ENV_PATH, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)},
               header="# 텔레그램 봇 (python -m smartfarm.notify.setup_bot 로 설정)")
    print(f"✔ 저장 완료: {ENV_PATH} (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
    try:
        send_message(token, chat_id, "✅ 스마트팜 봇 등록 완료\n\n/help 로 명령어를 확인하세요.")
    except RuntimeError as e:
        print(f"(확인 메시지 전송 실패: {e})")

    r = subprocess.run(["systemctl", "--user", "restart", "smartfarm-bot", "smartfarm"], check=False)
    print("✔ 서비스 재시작 완료 (smartfarm-bot, smartfarm)" if r.returncode == 0
          else "서비스 재시작 실패 — systemctl --user restart smartfarm-bot smartfarm")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n취소했습니다.")
        sys.exit(1)
