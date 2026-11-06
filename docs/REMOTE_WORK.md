# 다른 PC에서 SSH로 이어서 작업하기

이 문서는 **새 컴퓨터에서 이 라즈베리파이에 접속해 스마트팜 프로젝트 작업을 이어가는 방법**을 설명합니다.

---

## 0. 장비 정보

| 항목 | 값 |
|---|---|
| 접속 주소 | `bsa@192.168.0.81` |
| 모델 | Raspberry Pi 3 Model B+ |
| OS | Raspberry Pi OS 64-bit (Debian 13 trixie) |
| Python | 3.13 (프로젝트는 `~/smartfarm/.venv` 가상환경 사용) |
| 프로젝트 경로 | `/home/bsa/smartfarm` |

> IP는 공유기 설정에 따라 바뀔 수 있습니다. 바뀌면 공유기 관리페이지에서
> MAC `b8:27:eb:9c:ea:97` 에 할당된 IP를 확인하세요. (고정 IP 할당 권장)

---

## 1. 새 PC에서 SSH 접속 설정

### 1-1. SSH 키 만들기 (새 PC에서, 최초 1회)
```bash
ssh-keygen -t ed25519 -C "새PC이름-smartfarm"
```
passphrase는 비워 두거나(자동 접속 편함), 보안을 위해 설정해도 됩니다.

### 1-2. 공개키를 라즈베리파이에 등록
**Windows PowerShell:**
```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh bsa@192.168.0.81 "cat >> ~/.ssh/authorized_keys"
```
**Linux / macOS:**
```bash
ssh-copy-id bsa@192.168.0.81
```
비밀번호를 한 번 입력하면 등록됩니다. 이후부터는 비밀번호 없이 접속됩니다.

### 1-3. 접속 테스트
```bash
ssh bsa@192.168.0.81
```

### (선택) 접속 별칭 설정 — `~/.ssh/config` 에 추가
```
Host pi-farm
    HostName 192.168.0.81
    User bsa
    IdentityFile ~/.ssh/id_ed25519
```
이후 `ssh pi-farm` 으로 짧게 접속할 수 있습니다.

---

## 2. 작업 환경 준비

접속 후 프로젝트 폴더로 이동:
```bash
cd ~/smartfarm
```

가상환경이 아직 없다면(새로 클론했거나 처음이면):
```bash
bash scripts/install.sh
```

가상환경 활성화:
```bash
source .venv/bin/activate
```

---

## 3. 코드 편집 방법 (3가지 중 택1)

### 방법 A — VS Code 원격 접속 (가장 편리, 권장)
새 PC의 VS Code에 **"Remote - SSH"** 확장을 설치한 뒤:
1. `F1` → `Remote-SSH: Connect to Host` → `bsa@192.168.0.81`
2. 열린 창에서 `File > Open Folder` → `/home/bsa/smartfarm`

라즈베리파이의 파일을 로컬처럼 편집·실행·디버깅할 수 있습니다.

### 방법 B — 터미널에서 직접 편집
```bash
nano src/smartfarm/main.py      # 또는 vim
```

### 방법 C — 로컬에서 편집 후 전송 (git 사용 권장, 아래 4번)

---

## 4. 여러 PC에서 코드 동기화 (git, 강력 권장)

여러 컴퓨터에서 작업한다면 git 으로 코드를 관리하는 것이 가장 안전합니다.

### 최초 1회 (라즈베리파이에서)
```bash
cd ~/smartfarm
git init
git add -A
git commit -m "init: smartfarm 프로젝트 구조"
```
그리고 GitHub/GitLab 비공개 저장소를 만들어 연결:
```bash
git remote add origin <저장소주소>
git push -u origin main
```

### 이후 각 PC에서
```bash
# 작업 시작 전: 최신 내려받기
git pull
# 작업 후: 올리기
git add -A && git commit -m "작업내용" && git push
```

> `.env`, `config/config.yaml`, `data/`, `logs/`, `.venv/` 는 `.gitignore`로
> 제외되어 **비밀값과 런타임 데이터는 git에 올라가지 않습니다.** 새 PC에서는
> `.env`, `config.yaml`을 각자 다시 만들어야 합니다(템플릿 복사).

---

## 5. 서버 상태 확인·제어 (자주 쓰는 명령)

```bash
# 서비스 상태
sudo systemctl status smartfarm
# 실시간 로그
journalctl -u smartfarm -f
# 재시작 / 중지 / 시작
sudo systemctl restart smartfarm
sudo systemctl stop smartfarm
sudo systemctl start smartfarm
# 수동 실행(디버깅용, 서비스 끈 상태에서)
source .venv/bin/activate && python -m smartfarm.main
```

---

## 6. 체크리스트 (새 PC에서 처음 이어받을 때)

- [ ] SSH 키 등록 완료 (1번)
- [ ] `ssh bsa@192.168.0.81` 접속 확인
- [ ] (git 사용 시) `git pull` 로 최신 코드 확보
- [ ] `source .venv/bin/activate`
- [ ] `.env`, `config/config.yaml` 존재 확인 (없으면 템플릿 복사 후 값 입력)
- [ ] `sudo systemctl status smartfarm` 로 서버 동작 확인
