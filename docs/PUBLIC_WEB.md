# 공개 웹 대시보드 (보기 전용)

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

- **GET/HEAD 만 허용**(그 외 405). 밸브 제어 등 변경 기능은 없다.
- 그 외 모든 경로는 404. 응답은 허용 목록 필드로 새로 구성하므로 GPIO 핀, 센서 주소,
  `.env`, 설정 파일, 로그는 공개되지 않는다.
- 센서가 미연결이거나 드라이버가 미구현이면 `simulated: true` → 화면에 **모의값** 표시.
- 측정 주기 × 3 이상 갱신이 없으면 상태가 "갱신 중단됨"으로 바뀐다.

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
PYTHONPATH=src .venv/bin/python -B -m unittest -v tests.test_public_server
```

9개 테스트: 화면·정적파일 제공, 공개 필드 허용 목록(핀·주소·토큰 미노출),
이력 기간 제한·다운샘플, 잘못된 파라미터 처리, 미지정 경로 404, 쓰기 메서드 405,
HEAD, DB 읽기 전용, 상태 파일 없음/오래됨 처리.
