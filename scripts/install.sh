#!/usr/bin/env bash
# 스마트팜 서버 설치 스크립트 (라즈베리파이)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "== 프로젝트 경로: $ROOT =="

echo "== 1) 시스템 패키지 설치 =="
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev i2c-tools git

echo "== 2) 파이썬 가상환경(.venv) 생성 =="
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

echo "== 3) 파이썬 패키지 설치 =="
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "== 4) 런타임 폴더 준비 =="
mkdir -p data logs

echo ""
echo "설치 완료! 다음 단계:"
echo "  cp .env.example .env                         # 텔레그램 토큰 입력"
echo "  cp config/config.example.yaml config/config.yaml   # 장비 구성"
echo "  bash scripts/run.sh                          # 수동 실행 테스트"
