# 텔레그램 봇

휴대폰 텔레그램에서 명령을 보내면 스마트팜 현재 상태를 답장한다.

| 명령 | 내용 |
|---|---|
| `/status` | 전체 상태: 측정 정상 여부, 센서 전체 값(⚠ 기준 이탈, 모의값 표시), 밸브 열림/닫힘, 서비스 3개 상태, Pi CPU 온도·가동 시간·메모리·디스크 |
| `/temp` | 온도센서별 현재값 + 최근 24시간 최저/최고, 온도 기준값 |
| `/alerts` | 지금 걸려 있는 경고 목록: 센서 기준 이탈, 측정 갱신 중단(측정 주기×3 이상), 센서 값 없음, 서비스 중지, CPU 75 °C 이상, 디스크 여유 1 GB 미만, 최근 1시간 로그 오류 |
| `/help` | 명령어 목록 (`/start` 도 같음) |

텔레그램 입력창의 `/` 메뉴에도 위 명령이 자동 등록된다(봇 시작 시 setMyCommands).

## 구조

```
텔레그램 서버 ⇄ (getUpdates 롱폴링, Pi 가 바깥으로만 접속) ⇄ smartfarm-bot 서비스
                                                         ├ data/status.json   현재값 (10초마다 갱신)
                                                         ├ data/smartfarm.db  24시간 최저/최고 (읽기 전용)
                                                         ├ config/config.yaml 기준값(rules)
                                                         └ logs/smartfarm.log 최근 오류 수
```

- 코드: `src/smartfarm/notify/bot.py`(수신·응답), `commands.py`(답장 내용), `telegram.py`(API 호출, 알림 전송)
- 서비스: `systemd/smartfarm-bot.service` → `~/.config/systemd/user/` (사용자 서비스, 부팅 시 자동 시작)
- 들어오는 포트·웹훅이 없으므로 공유기·Funnel 설정이 필요 없다.
- 읽기 전용: 밸브 제어 등 쓰기 명령은 없다.

## 보안

- `.env` 의 `TELEGRAM_CHAT_ID` 에 등록된 대화에만 답한다. 쉼표로 여러 개 등록 가능(`123,456`).
- 등록되지 않은 사람이 명령을 보내면 "등록되지 않은 사용자 + 그 사람의 chat id" 만 답하고 측정값은 보내지 않는다
  (가족 등을 추가할 때 그 chat id 를 `.env` 에 넣으면 된다).
- 토큰은 로그에 남지 않도록 가린다(`/bot<redacted>`). `.env` 권한은 600.
- 봇이 꺼져 있던 동안 쌓인 2분 넘은 명령은 답하지 않고 건너뛴다.

## 설정 (처음 한 번)

1. 텔레그램에서 **@BotFather** → `/newbot` → 봇 이름·아이디 정하기 → 토큰(`123456789:AA…`) 복사
2. PC PowerShell 에서 (비밀값 입력이라 사용자가 직접 실행):

   ```powershell
   ssh -t smartfarm "cd ~/smartfarm && PYTHONPATH=src .venv/bin/python -m smartfarm.notify.setup_bot"
   ```

   - 토큰 붙여넣기(화면에 표시되지 않음) → 봇 확인
   - 휴대폰에서 새 봇에게 `/start` 보내기 → chat id 자동 확인 → `Y`
   - `.env` 저장 → `smartfarm-bot`, `smartfarm` 재시작 → 휴대폰에 "등록 완료" 메시지 도착
3. 휴대폰에서 `/status` 를 보내 확인

토큰이 없으면 봇 서비스는 오류 없이 30초마다 `.env` 를 다시 확인하며 기다린다.
메인 서버의 시작 알림(🌱 스마트팜 서버 시작됨)도 같은 토큰·chat id 로 전송된다.

## 운영 명령

```bash
systemctl --user status smartfarm-bot
journalctl --user -u smartfarm-bot -f        # 또는 logs/smartfarm.log (관리자 페이지 로그에도 표시)
systemctl --user restart smartfarm-bot       # .env 수동 수정 후
```

## 테스트

```bash
PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_bot     # 17개, 네트워크 없음
```

## 자동 경보 (요청하지 않아도 전송)

측정 서버(`smartfarm`)가 10초마다 판단해 등록된 대화로 보낸다. 코드: `src/smartfarm/alerts.py`.

| 알림 | 조건 |
|---|---|
| 🔴 [경보] | 기준(`rules`)을 **연속 3회(30초)** 벗어남 — 한 번 튀는 값은 무시 |
| 🔴 [경보 지속] | 이탈이 계속되면 **60분마다** 다시 알림 |
| ✅ [복구] | 기준 안쪽으로 여유값만큼(온도 0.5 °C) 들어온 상태가 연속 3회 — 기준선 근처에서 경보/복구가 반복되지 않음 |
| ⚠ [센서 오류] | 센서 값을 연속 3회 못 읽음 (선 빠짐, CRC 오류, 85 °C 리셋값 등) |
| ✅ [센서 복구] | 다시 정상 측정 |
| 🕒 정기 보고 | **12시간마다** 온도와 경고 요약 |
| 🌱 서버 시작됨 | 측정 서버가 시작될 때 (Pi 재부팅 포함) |

예: `🔴 [경보] temp_1 온도 31.2 °C — 최대 30.0 °C 초과`

- 아직 드라이버가 없는 **모의값 센서(습도/pH/EC)는 경보에서 제외**한다(`include_simulated: false`).
  실제 센서를 연결하면 자동으로 포함된다.
- 인터넷이 끊겨 있으면 전송을 약 9분 동안 재시도한다. 측정은 전송을 기다리지 않는다.
- 기준값은 `config/config.yaml` 의 `rules`, 경보 설정은 `alerts` 에서 바꾼 뒤 `systemctl --user restart smartfarm`.

```yaml
alerts:
  on_threshold_breach: true      # 자동 경보 켜기/끄기
  confirm_count: 3               # 연속 몇 회 이탈 시 경보
  remind_minutes: 60             # 재알림 간격 (0 = 끔)
  include_simulated: false
  hysteresis: { temperature: 0.5, humidity: 2.0, ph: 0.1, ec: 0.1 }
  heartbeat_hours: 12            # 정기 보고 (0 = 끔)
```

- 이 Pi 자체가 꺼지거나 인터넷이 끊기면 알림을 보낼 수 없다. 정기 보고가 오지 않으면 Pi 상태를 확인한다.

## 참고

- 온도센서 주소가 설정된 센서는 선이 빠져도 모의값으로 바뀌지 않고 "값 없음"이 된다(센서 오류 경보).
- `/temp` 의 24시간 최저/최고에는 2026-11-07 14:30 이전 모의 데이터가 섞여 있다(24시간 지나면 자연히 빠짐).
- 밸브 자동 제어는 아직 없다(`main.py` 의 `evaluate_rules()`).
