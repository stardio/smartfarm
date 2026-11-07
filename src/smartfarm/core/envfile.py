""".env 파일 읽기/부분 갱신 도우미.

다른 줄(주석·다른 키)은 그대로 두고 지정한 키만 바꾼다. 결과 파일 권한은 600.
"""
from __future__ import annotations

import os
from pathlib import Path


def is_placeholder(value: str | None) -> bool:
    """비어 있거나 .env.example 의 템플릿 값(예: '여기에_봇_토큰')이면 True."""
    v = (value or "").strip()
    return not v or v.startswith("여기에") or v.lower() in ("changeme", "your_token_here")


def update_env(env_path: Path, values: dict[str, str], header: str | None = None) -> None:
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
        if header:
            out += ["", header]
        out += [f"{k}={values[k]}" for k in missing]
    tmp = env_path.with_name(env_path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)
