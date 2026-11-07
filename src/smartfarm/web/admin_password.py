"""관리자 비밀번호 설정/변경.

    cd ~/smartfarm && PYTHONPATH=src .venv/bin/python -m smartfarm.web.admin_password
    systemctl --user restart smartfarm-web

비밀번호는 화면에 표시되지 않고, scrypt 해시만 .env 의 ADMIN_PASSWORD_HASH 에 저장된다.
변경하면 웹서버 재시작 시 기존 로그인 세션은 모두 끊긴다.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from .admin import MIN_PASSWORD_LEN, hash_password

ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT / ".env"
KEY = "ADMIN_PASSWORD_HASH"


def write_hash(env_path: Path, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == KEY:
            if not done:
                out.append(f"{KEY}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out += ["", "# 관리자 페이지 비밀번호 해시 (python -m smartfarm.web.admin_password 로 설정)", f"{KEY}={value}"]
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)


def main() -> int:
    print("스마트팜 관리자 비밀번호 설정 (입력 내용은 화면에 표시되지 않습니다)")
    pw = getpass.getpass("새 비밀번호: ")
    if len(pw) < MIN_PASSWORD_LEN:
        print(f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다.", file=sys.stderr)
        return 1
    if getpass.getpass("한 번 더 입력: ") != pw:
        print("두 입력이 일치하지 않습니다.", file=sys.stderr)
        return 1
    write_hash(ENV_PATH, hash_password(pw))
    print(f"저장 완료: {ENV_PATH} ({KEY})")
    print("적용: systemctl --user restart smartfarm-web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
