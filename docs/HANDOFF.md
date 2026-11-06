# 작업 인계 문서 (HANDOFF)

> 사무실 PC → 현장 PC 로 개발을 이어가기 위한 현재 상태 요약.
> 현장 PC의 Claude Code 세션은 이 파일을 먼저 읽고 맥락을 파악할 것.
> 최종 갱신: 2026-11-06

## 장비
- Raspberry Pi 3 Model B+, 사용자 `bsa`, Raspberry Pi OS 64-bit (Debian 13, Python 3.13)
- 사무실망 IP: 192.168.0.81 (현장 설치 후 바뀜 → Tailscale 이름으로 접속 권장)
- 프로젝트: `/home/bsa/smartfarm` (이 저장소)

## 지금까지 완료된 것
- [x] 프로젝트 구조/문서/스켈레톤 생성, GitHub 푸시 (git@github.com:stardio/smartfarm.git)
- [x] Python venv(.venv) + 핵심 패키지 설치 (PyYAML, dotenv, requests, smbus2, gpiozero)
- [x] 사용자 systemd 서비스 등록 + linger (부팅 자동 실행). 관리: `systemctl --user ... smartfarm`
- [x] 서버 구동 확인 (센서 모의값→SQLite 기록, 밸브 제어 추상화, 정상 종료)
- [x] DS18B20 온도센서 3개 지원 구현 (1-Wire, GPIO4 공용 버스, sysfs 읽기, 미연결시 모의값)

## 접속 방법
- 현장/외부: **Tailscale VPN** 사용 (RTK Base와 동일 tailnet). 아래 "남은 작업" 참고.
  설정 후: `ssh bsa@smartfarm-pi`
- 현장 로컬망: `ssh bsa@<현장IP>`
- git push/pull 은 Pi의 deploy key(`~/.ssh/github_smartfarm`)로 인증됨 — 추가 설정 불필요.

## 남은 작업 (TODO)
1. **Tailscale 합류** (sudo 필요, 현장에서):
   `curl -fsSL https://tailscale.com/install.sh | sh`
   `sudo tailscale up --hostname=smartfarm-pi`  (RTK Base와 **같은 계정**으로 승인)
2. **1-Wire 활성화 + 센서 주소 등록** (sudo, 배선 후):
   `sudo raspi-config nonint do_onewire 0 && sudo reboot`
   `bash scripts/detect_ds18b20.sh`  → 나온 28-... 주소를 `config/config.yaml` temp_1/2/3 에 입력
   `systemctl --user restart smartfarm`
3. **GPIO 백엔드** (밸브 실제 제어, sudo): `sudo apt install -y python3-lgpio`
4. **텔레그램 설정**: `nano .env` 에 BOT_TOKEN/CHAT_ID → restart
5. **습도/pH/EC 드라이버 실제 구현** (`src/smartfarm/sensors/humidity.py, ph.py, ec.py`의 TODO)
6. **자동제어 로직 구현**: `src/smartfarm/main.py` 의 `evaluate_rules()` — 임계값 이탈시 밸브 제어 + 텔레그램 경보
7. (참고) RTK Base 폴더 `~/raspberrypi_RTK_base/` 에 재활용 가능한 DS18B20 다중센서 코드 있음.

## 현장 PC 최초 셋업
```bash
git clone https://github.com/stardio/smartfarm.git   # 참고용 로컬 사본(실작업은 Pi에서)
ssh bsa@smartfarm-pi            # Tailscale 합류 후
# 또는 VS Code Remote-SSH 로 /home/bsa/smartfarm 열기 (docs/REMOTE_WORK.md 참고)
```
