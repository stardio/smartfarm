#!/usr/bin/env bash
# 스마트팜 서버 수동 실행 (디버깅용)
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="$(pwd)/src"
exec python -m smartfarm.main
