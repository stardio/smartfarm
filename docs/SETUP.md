# 설치 및 서비스 등록 가이드

## 1. 시스템 준비 (최초 1회)

```bash
cd ~/smartfarm
bash scripts/install.sh
```

`install.sh` 가 하는 일:
- 필요한 시스템 패키지 설치 (`python3-venv`, `i2c-tools`, `git` 등)
- `~/smartfarm/.venv` 가상환경 생성
- `requirements.txt` 의 파이썬 패키지 설치

## 2. I2C / GPIO 활성화

pH·EC·일부 온습도 센서는 I2C를 사용합니다. 활성화:
```bash
sudo raspi-config nonint do_i2c 0      # I2C 켜기
sudo reboot                            # 재부팅 후 적용
```
확인:
```bash
i2cdetect -y 1        # 연결된 I2C 장치 주소가 표에 보여야 함
```

## 3. 비밀값 / 설정 입력

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
nano .env             # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
nano config/config.yaml   # 센서 개수/주소/핀, 밸브 GPIO 핀, 임계값
```

### 텔레그램 봇 토큰 얻기
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 봇 이름 지정 → **토큰** 수령
2. 만든 봇과 대화 시작(아무 메시지 전송)
3. `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → `chat.id` 확인
4. 토큰과 chat id 를 `.env` 에 입력

## 4. 수동 실행 테스트

```bash
source .venv/bin/activate
python -m smartfarm.main
```
정상이면 텔레그램으로 시작 메시지가 오고, 로그가 출력됩니다. `Ctrl+C` 로 종료.

## 5. 서비스 등록 (부팅 시 자동 실행)

현재 장비는 **사용자(--user) systemd 서비스 + linger** 방식으로 등록되어 있습니다.
(`systemd/smartfarm.service` 는 시스템 서비스용이라, 사용자 서비스로 쓸 때는 `User=` 줄을
빼고 `WantedBy=default.target` 으로 바꿔 설치합니다.)

```bash
# 서비스 파일 설치 (사용자 서비스)
mkdir -p ~/.config/systemd/user
sed -e '/^User=/d' -e 's/multi-user.target/default.target/'     systemd/smartfarm.service > ~/.config/systemd/user/smartfarm.service
systemctl --user daemon-reload
systemctl --user enable --now smartfarm   # 부팅 자동 실행 등록 + 지금 시작
sudo loginctl enable-linger bsa           # 로그인 없이도 부팅 시 실행 (최초 1회, sudo)
systemctl --user status smartfarm         # 상태 확인
```

로그 보기:
```bash
journalctl --user -u smartfarm -f     # 또는 tail -f logs/smartfarm.log
```

## 6. 업데이트 반영

코드 변경 후:
```bash
systemctl --user restart smartfarm
```
패키지 추가 시:
```bash
source .venv/bin/activate
pip install <패키지>
pip freeze > requirements.txt
```
