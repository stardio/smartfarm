"""관리자 비밀번호 설정/변경.

    cd ~/smartfarm && PYTHONPATH=src .venv/bin/python -m smartfarm.web.admin_password
    systemctl --user restart smartfarm-web

형식: 숫자 4자리 PIN 또는 10자 이상 비밀번호.
비밀번호는 화면에 표시되지 않고, scrypt 해시만 .env 의 ADMIN_PASSWORD_HASH 에 저장된다.
형식(pin/password)은 ADMIN_PASSWORD_KIND 에 저장되어 로그인 화면 입력 방식에 쓰인다.
변경하면 웹서버 재시작 시 기존 로그인 세션은 모두 끊긴다.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from .admin import MIN_PASSWORD_LEN, hash_password, secret_kind

ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT / ".env"
KEY = "ADMIN_PASSWORD_HASH"
KIND_KEY = "ADMIN_PASSWORD_KIND"


def write_hash(env_path: Path, value: str, kind: str = "password") -> None:
    values = {KEY: value, KIND_KEY: kind}
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out, done = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in values:
            if k not in done:
                out.append(f"{k}={values[k]}")
                done.add(k)
            continue
        out.append(line)
    missing = [k for k in values if k not in done]
    if missing:
        if KEY in missing:
            out += ["", "# 관리자 페이지 비밀번호 (python -m smartfarm.web.admin_password 로 설정)"]
        out += [f"{k}={values[k]}" for k in missing]
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)


def main() -> int:
    print("스마트팜 관리자 비밀번호 설정 (입력 내용은 화면에 표시되지 않습니다)")
    print(f"  형식: 숫자 4자리 PIN (예: 4827)  또는  {MIN_PASSWORD_LEN}자 이상 비밀번호")
    pw = getpass.getpass("새 비밀번호/PIN: ")
    kind = secret_kind(pw)
    if kind is None:
        print(f"숫자 4자리 또는 {MIN_PASSWORD_LEN}자 이상으로 입력하세요.", file=sys.stderr)
        return 1
    if kind == "pin" and (len(set(pw)) == 1 or pw in ("1234", "4321", "0123", "9876")):
        print("너무 쉬운 PIN 입니다(같은 숫자 반복·연속 숫자). 다른 숫자로 정하세요.", file=sys.stderr)
        return 1
    if getpass.getpass("한 번 더 입력: ") != pw:
        print("두 입력이 일치하지 않습니다.", file=sys.stderr)
        return 1
    write_hash(ENV_PATH, hash_password(pw), kind)
    print(f"저장 완료: {ENV_PATH} ({KEY}, 형식={'PIN 4자리' if kind == 'pin' else '비밀번호'})")
    print("적용: systemctl --user restart smartfarm-web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
