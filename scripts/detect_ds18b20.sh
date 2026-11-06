#!/usr/bin/env bash
# 연결된 DS18B20 센서의 주소와 현재 온도를 출력
set -euo pipefail
W1=/sys/bus/w1/devices
if [ ! -d "$W1" ]; then
  echo "1-Wire 버스가 비활성입니다. 먼저:"
  echo "  sudo raspi-config nonint do_onewire 0 && sudo reboot"
  exit 1
fi
found=0
for d in "$W1"/28-*; do
  [ -e "$d" ] || continue
  addr=$(basename "$d")
  raw=$(cat "$d/w1_slave" 2>/dev/null || true)
  t=$(echo "$raw" | grep -o "t=[0-9]*" | cut -d= -f2)
  if [ -n "${t:-}" ]; then
    printf "%s  →  %.3f °C\n" "$addr" "$(echo "scale=3;$t/1000"|bc)"
  else
    printf "%s  (읽기 실패)\n" "$addr"
  fi
  found=$((found+1))
done
[ "$found" -eq 0 ] && echo "감지된 DS18B20 없음 (배선/풀업저항 확인)"
echo "총 ${found}개 감지됨"
