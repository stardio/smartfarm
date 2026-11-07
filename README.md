# 스마트팜 서버 (Smart Farm Server)

Raspberry Pi 3 Model B+ 에서 동작하는 스마트팜 제어 서버입니다.

## 기능
- **센싱**: 온도 / 습도 / pH / EC 센서 (각각 여러 개) 주기적 측정
- **제어**: 여러 개의 밸브(valve) 개폐 제어
- **알림**: 텔레그램(Telegram)으로 상태·경보 메시지 전송
- **저장**: 측정값을 로컬 SQLite DB에 기록
- **상시 구동**: systemd 서비스로 부팅 시 자동 실행
- **공개 웹 대시보드**: 누구나 볼 수 있는 보기 전용 현황 페이지 → https://smartfarm-pi.tail084a13.ts.net/ ([docs/PUBLIC_WEB.md](docs/PUBLIC_WEB.md))

## 빠른 시작
```bash
cd ~/smartfarm
bash scripts/install.sh        # 시스템 패키지 + 파이썬 venv 설치
cp .env.example .env           # 텔레그램 토큰 등 비밀값 입력
cp config/config.example.yaml config/config.yaml   # 센서/밸브/핀맵 설정
nano .env                      # BOT_TOKEN, CHAT_ID 입력
nano config/config.yaml        # 장비 구성 입력
bash scripts/run.sh            # 수동 실행 테스트
```

상시 구동(부팅 자동 실행)은 [docs/SETUP.md](docs/SETUP.md) 참고.

## 폴더 구조
```
smartfarm/
├── README.md                 # 이 파일
├── docs/
│   ├── SETUP.md              # 설치 / 서비스 등록 가이드
│   ├── REMOTE_WORK.md        # 다른 PC에서 SSH로 이어서 작업하는 법 ★
│   ├── HARDWARE.md           # 센서/밸브 배선·핀맵 기록
│   └── PUBLIC_WEB.md         # 공개 웹 대시보드 구성·운영
├── config/
│   └── config.example.yaml   # 장비 구성 템플릿 (실제 config.yaml 은 git 제외)
├── src/smartfarm/
│   ├── main.py               # 엔트리포인트(측정→판단→제어→알림 루프)
│   ├── core/                 # 설정 로더, 로깅
│   ├── sensors/              # 온도/습도/pH/EC 센서 드라이버
│   ├── actuators/            # 밸브 제어
│   ├── notify/               # 텔레그램 전송
│   ├── storage/              # SQLite 저장
│   ├── status.py             # 현재 상태 스냅샷(data/status.json) 기록
│   └── web/                  # 공개(보기 전용) 웹 대시보드 서버 + 화면
├── scripts/                  # install.sh, run.sh
├── systemd/                  # smartfarm.service, smartfarm-web.service (자동 실행)
├── tests/
├── data/                     # 런타임 DB (git 제외)
├── logs/                     # 로그 (git 제외)
├── requirements.txt
├── .env.example              # 비밀값 템플릿 (실제 .env 는 git 제외)
└── .gitignore
```

## 장비 정보
- 호스트: `bsa@172.30.1.19` (현장 로컬망) / `bsa@100.96.75.45` (Tailscale `smartfarm-pi`)
- 모델: Raspberry Pi 3 Model B+
- OS: Raspberry Pi OS 64-bit (Debian 13 trixie), Python 3.13

> 다른 컴퓨터에서 이어서 작업하려면 반드시 **[docs/REMOTE_WORK.md](docs/REMOTE_WORK.md)** 를 먼저 읽으세요.
