# 작업 인계 문서 (HANDOFF)

> 사무실 PC → 현장 PC 로 개발을 이어가기 위한 현재 상태 요약.
> 현장 PC의 Claude Code 세션은 이 파일을 먼저 읽고 맥락을 파악할 것.
> 최종 갱신: 2026-11-07

## 장비
- Raspberry Pi 3 Model B+, 사용자 `bsa`, Raspberry Pi OS 64-bit (Debian 13, Python 3.13)
- 데스크톱(GUI) 끔: 콘솔 부팅(multi-user.target, 2026-11-07). SSH 전용. 되돌리기: `sudo raspi-config nonint do_boot_behaviour B4`
- 현장 로컬망 IP: `172.30.1.19` (eth0, MAC `b8:27:eb:c9:bf:c2`) — 공유기에서 고정 할당 권장
- Tailscale: 이름 `smartfarm-pi`, IP `100.96.75.45` (RTK Base `rtk-base-pi` 와 동일 tailnet)
- (이전) 사무실망 IP: 192.168.0.81
- 프로젝트: `/home/bsa/smartfarm` (이 저장소)

## 지금까지 완료된 것
- [x] 프로젝트 구조/문서/스켈레톤 생성, GitHub 푸시 (git@github.com:stardio/smartfarm.git)
- [x] Python venv(.venv) + 핵심 패키지 설치 (PyYAML, dotenv, requests, smbus2, gpiozero)
- [x] 사용자 systemd 서비스 등록 + linger (부팅 자동 실행). 관리: `systemctl --user ... smartfarm`
- [x] 서버 구동 확인 (센서 모의값→SQLite 기록, 밸브 제어 추상화, 정상 종료)
- [x] DS18B20 온도센서 3개 지원 구현 (1-Wire, GPIO4 공용 버스, sysfs 읽기, 미연결시 모의값)
- [x] 현장 PC SSH 키 등록 + 접속 확인 (2026-11-07)
- [x] Tailscale 설치·합류, 부팅 자동 시작 (2026-11-07)
- [x] 공개(보기 전용) 웹 대시보드 `smartfarm-web` 서비스 (127.0.0.1:8081) + Tailscale Funnel 공개 (2026-11-07)
      → https://smartfarm-pi.tail084a13.ts.net/  상세: [PUBLIC_WEB.md](PUBLIC_WEB.md)
- [x] 관리자 페이지 `/admin` (비밀번호 로그인, 보기 전용 상세 정보) (2026-11-07)
      비밀번호 설정(숫자 4자리 PIN 또는 10자 이상): `PYTHONPATH=src .venv/bin/python -m smartfarm.web.admin_password`
      ⚠ 현재 테스트용 4자리 PIN(로그인 잠금 없음). **운영 전 10자 이상 비밀번호로 변경**하면 잠금 자동 적용.

## 접속 방법
- 공개 대시보드(누구나, 보기 전용): https://smartfarm-pi.tail084a13.ts.net/
- 현장 로컬망: `ssh bsa@172.30.1.19`
- 현장/외부 (Tailscale): `ssh bsa@100.96.75.45` (또는 MagicDNS 이름 `smartfarm-pi`)
- 현장 PC `~/.ssh/config` 별칭: `smartfarm` (로컬망), `smartfarm-ts` (Tailscale)
- git push/pull 은 Pi의 deploy key(`~/.ssh/github_smartfarm`)로 인증됨 — 추가 설정 불필요.

## 남은 작업 (TODO)
1. ~~1-Wire 활성화 + 센서 주소 등록~~ → 완료(2026-11-07). DS18B20 3개 실측 중, 주소는 docs/HARDWARE.md.
   센서 위치(어느 주소가 어디 설치)는 아직 미기록 — 확인 후 HARDWARE.md 비고에 적을 것.
2. **GPIO 백엔드 연결** (밸브 실제 제어): `python3-lgpio` 는 apt 로 설치되어 있으나
   `.venv` 가 `include-system-site-packages = false` 라 venv 에서 import 되지 않음.
   → venv 에서 시스템 패키지를 보이게 하거나 venv 재생성 필요.
   **주의: 적용 후 재시작하면 배선된 밸브가 실제로 동작함.**
3. **텔레그램 설정**: 봇(`/status /temp /alerts /help`) 코드·서비스(smartfarm-bot)는 완료(2026-11-07).
   토큰·chat id 만 입력하면 동작 → `ssh -t smartfarm "cd ~/smartfarm && PYTHONPATH=src .venv/bin/python -m smartfarm.notify.setup_bot"`
   (docs/TELEGRAM.md)
4. **습도/pH/EC 드라이버 실제 구현** (`src/smartfarm/sensors/humidity.py, ph.py, ec.py`의 TODO)
5. **자동제어 로직 구현**: 텔레그램 자동 경보는 완료(2026-11-07, src/smartfarm/alerts.py). 남은 것은 `main.py` 의 `evaluate_rules()` — 임계값 이탈시 밸브 제어
6. (참고) RTK Base 폴더 `~/raspberrypi_RTK_base/` 에 재활용 가능한 DS18B20 다중센서 코드 있음.

## 새 PC 최초 셋업
```bash
git clone https://github.com/stardio/smartfarm.git   # 참고용 로컬 사본(실작업은 Pi에서)
ssh bsa@172.30.1.19             # 로컬망 / 외부에서는 ssh bsa@100.96.75.45 (Tailscale)
# 또는 VS Code Remote-SSH 로 /home/bsa/smartfarm 열기 (docs/REMOTE_WORK.md 참고)
```
