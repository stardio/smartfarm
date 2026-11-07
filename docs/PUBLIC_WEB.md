# 공개 웹 대시보드 (보기 전용) + 관리자 페이지

누구나 인터넷에서 스마트팜 현황을 볼 수 있는 페이지. RTK Base(`rtk-base-pi`)의
공개 페이지와 같은 방식(Tailscale Funnel → 루프백 전용 서버)으로 구성한다.

공개 주소: `https://smartfarm-pi.tail084a13.ts.net/`

## 연결 구조

```
인터넷 브라우저 → Tailscale Funnel (HTTPS 443) → Pi 127.0.0.1:8081 (smartfarm-web)
                                                    ├─ data/status.json   (메인 서버가 측정 주기마다 기록)
                                                    └─ data/smartfarm.db  (읽기 전용 mode=ro 로 열기)
```

- 웹서버: `src/smartfarm/web/public_server.py` (표준 라이브러리만 사용)
- 화면: `src/smartfarm/web/static/` (`index.html`, `app.css`, `app.js` — 외부 폰트·CDN·추적 도구 없음)
- 상태 스냅샷: `src/smartfarm/status.py` → `data/status.json` (메인 루프에서 원자적 기록)
- 메인 서버(`smartfarm`)와 웹서버(`smartfarm-web`)는 별도 프로세스. 웹서버가 죽어도 측정·제어에 영향 없음.

## 공개 범위

| 경로 | 내용 |
|---|---|
| `/` | 대시보드 화면 |
| `/api/public/status` | 센서 현재값(이름·종류·값·모의값 여부), 밸브 열림/닫힘, 정상 범위, 수신 상태 |
| `/api/public/history?hours=6\|24\|72` | 센서별 구간 평균(최대 144점) |
| `/api/v1/health` | 수신 상태와 마지막 측정 시각만 |

- 공개 경로는 **GET/HEAD 만 허용**(그 외 405). 밸브 제어 등 변경 기능은 없다.
- 그 외 모든 경로는 404. 응답은 허용 목록 필드로 새로 구성하므로 GPIO 핀, 센서 주소,
  `.env`, 설정 파일, 로그는 공개되지 않는다.
- 센서가 미연결이거나 드라이버가 미구현이면 `simulated: true` → 화면에 **모의값** 표시.
- 측정 주기 × 3 이상 갱신이 없으면 상태가 "갱신 중단됨"으로 바뀐다.

## 관리자 페이지 `/admin`

공개 주소의 `https://smartfarm-pi.tail084a13.ts.net/admin` 에서 비밀번호로 로그인한다.
로그인 후 볼 수 있는 것(보기 전용): 서비스 상태(smartfarm / smartfarm-web), 센서 수신,
CPU 온도·가동시간·부하·메모리·디스크, DB 건수·기간, 센서 주소/인터페이스, 밸브 GPIO 핀·구동 방식,
자동제어 기준·알림 설정, 최근 로그 80줄(토큰·비밀번호 형태 값은 `<redacted>` 로 가림).

| 경로 | 메서드 | 인증 |
|---|---|---|
| `/admin`, `/admin.js` | GET | 없음(로그인 화면) |
| `/admin/api/session` | GET | 없음(설정·로그인·잠금 여부만) |
| `/admin/api/login` | POST | 같은 사이트 Origin 필수 |
| `/admin/api/logout` | POST | 같은 사이트 Origin 필수 |
| `/admin/api/overview` | GET | 로그인 세션 필수(없으면 401) |

### 비밀번호 설정/변경 (Pi 에서, 직접 입력)

```bash
cd ~/smartfarm
PYTHONPATH=src .venv/bin/python -m smartfarm.web.admin_password   # 숫자 4자리 PIN 또는 10자 이상, 화면에 표시 안 됨
systemctl --user restart smartfarm-web
```

- 형식: **숫자 4자리 PIN**(같은 숫자 반복·1234 등 연속 숫자는 거부) 또는 **10자 이상 비밀번호**.
  PIN 이면 로그인 화면에 숫자 키패드가 뜨고 4자리 입력 시 자동 로그인.
- `.env` 의 `ADMIN_PASSWORD_HASH` 에 **scrypt 해시만** 저장, 형식은 `ADMIN_PASSWORD_KIND`(pin/password)
  (평문 저장 안 함, `.env` 는 git 제외, 권한 600).
- 해시가 없으면 관리자 로그인은 비활성(503)이고 화면에 설정 방법이 표시된다.
- 웹서버 재시작 시 모든 로그인 세션이 끊긴다(세션은 메모리에만 보관).

### 보안 동작

- 세션 쿠키 `__Host-sf_admin`: HttpOnly · Secure · SameSite=Strict · Path=/. 2시간 미사용 또는 로그인 12시간 후 만료.
- 로그인/로그아웃 POST 는 `Origin` 이 접속 호스트와 같을 때만 처리(CSRF 방지), 본문 2KB 제한.
- 실패 시 1초 지연, **15분 안에 5회 실패하면 로그인 전체 잠금**(올바른 비밀번호도 거부).
  잠금 시간은 잠길 때마다 2배: 15분 → 30분 → 1시간 → … **최대 24시간**. 로그인에 성공하거나
  24시간 동안 실패가 없으면 초기화. PIN 은 경우의 수가 1만 개뿐이라 이 단계적 잠금이 핵심 방어다
  (잠금 상한 도달 후에는 하루 5회만 시도 가능 → 전체 대입에 수년).
  Funnel 뒤에서는 접속자 IP 를 신뢰할 수 없어 IP 별이 아닌 전역 제한이다.
  → 누군가 일부러 계속 틀리면 관리자도 잠시 로그인할 수 없다. 그때는 SSH 로 접속하거나
  `systemctl --user restart smartfarm-web` 으로 잠금을 풀 수 있다.
- 비밀번호 해시 비교는 상수 시간(`hmac.compare_digest`).

## 서비스

사용자 서비스 `smartfarm-web.service` (linger 로 부팅 시 자동 시작).

```bash
cp systemd/smartfarm-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now smartfarm-web
systemctl --user status smartfarm-web
curl -s http://127.0.0.1:8081/api/v1/health
```

## 외부 공개 (Tailscale Funnel, 최초 1회)

```bash
sudo tailscale funnel --bg --yes http://127.0.0.1:8081
tailscale funnel status
```

- `--bg` 설정은 Tailscale 재시작·재부팅 후에도 유지된다.
- 최초 실행 시 Tailscale 관리자 웹에서 Funnel 승인이 요구될 수 있다.
- 공유기 포트포워딩·방화벽은 변경하지 않는다(80/443 은 RTK Base 가 사용 중).

## 공개 중단

```bash
sudo tailscale funnel --https=443 off            # 공개만 중단
systemctl --user disable --now smartfarm-web     # 웹서버까지 중단
```

## 검증

```bash
PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_public_server tests.test_admin
```

관리자 15개 테스트(PIN 형식·단계적 잠금 포함): 로그인 화면, 미로그인 401, 오답, 로그인→상세→로그아웃(쿠키 속성·로그 가림 포함),
위조 쿠키, 타 사이트 Origin 403, 본문 크기 413, 반복 실패 잠금, 미설정 503, 해시 검증, `.env` 갱신.

공개 9개 테스트: 화면·정적파일 제공, 공개 필드 허용 목록(핀·주소·토큰 미노출),
이력 기간 제한·다운샘플, 잘못된 파라미터 처리, 미지정 경로 404, 쓰기 메서드 405,
HEAD, DB 읽기 전용, 상태 파일 없음/오래됨 처리.
