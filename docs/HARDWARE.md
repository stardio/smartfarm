# 하드웨어 배선 / 핀맵 기록

> 실제 배선을 여기에 기록해 두면, 다른 PC/다른 사람이 이어받을 때 혼선이 없습니다.
> 값은 예시이며 실제 구성에 맞게 수정하세요. 코드 설정은 `config/config.yaml` 에 반영합니다.

## 라즈베리파이 핀 번호 규칙
- 이 프로젝트는 **BCM(GPIO) 번호** 기준으로 핀을 지정합니다. (물리 핀 번호 아님)
- 핀 상태 확인: `pinctrl get` / 특정 핀: `pinctrl get 17`

## 센서 목록

### 온도 센서 — DS18B20 x3 (1-Wire)
3개 모두 **GPIO4(물리 7번)** 1-Wire 버스를 공유한다. 각 센서는 고유 주소로 구분.

| 이름 | 종류 | 버스핀(BCM) | 주소(ROM) | 비고 |
|---|---|---|---|---|
| temp_1 | DS18B20 | GPIO4 | 28-000000d6c59d | 2026-11-07 연결 확인 |
| temp_2 | DS18B20 | GPIO4 | 28-000000d7809f | 2026-11-07 연결 확인 |
| temp_3 | DS18B20 | GPIO4 | 28-000000d7ab56 | 2026-11-07 연결 확인 |

**배선 (3개 공통/병렬):**
- VDD  → 3.3V (물리 1번)
- GND  → GND  (물리 6/9/14/20/25/30/34/39번 중 하나)
- DATA → GPIO4 (물리 7번)
- **풀업 저항 4.7kΩ 1개**: DATA(GPIO4) ↔ 3.3V 사이 (버스 전체에 1개만)

**활성화 & 주소 확인:**
```bash
sudo raspi-config nonint do_onewire 0   # 1-Wire(GPIO4) 켜기
sudo reboot
bash scripts/detect_ds18b20.sh          # 연결된 28-... 주소 목록 출력
```
확인한 주소를 config.yaml 의 temp_1/2/3 address 에 입력 후:
`systemctl --user restart smartfarm`

### 습도 센서
| 이름 | 종류 | 연결 방식 | 주소/핀 | 비고 |
|---|---|---|---|---|
| humi_1 | SHT31 | I2C | 0x44 | temp_2 와 동일 모듈 |

### pH 센서
| 이름 | 종류 | 연결 방식 | 주소 | 보정 |
|---|---|---|---|---|
| ph_1 | (예: DFRobot + ADS1115) | I2C(ADC) | 0x48 | 2점 보정(4.0/7.0) |

### EC 센서
| 이름 | 종류 | 연결 방식 | 주소 | 보정 |
|---|---|---|---|---|
| ec_1 | (예: DFRobot + ADS1115) | I2C(ADC) | 0x49 | 온도보정 적용 |

## 밸브 (액추에이터)
| 이름 | 제어 방식 | GPIO(BCM) | active | 용도 |
|---|---|---|---|---|
| valve_1 | 릴레이 | GPIO17 | HIGH/LOW | 양액 공급 |
| valve_2 | 릴레이 | GPIO27 | HIGH/LOW | 배수 |
| valve_3 | 릴레이 | GPIO22 | HIGH/LOW | 관수 A |
| valve_4 | 릴레이 | GPIO23 | HIGH/LOW | 관수 B |

> 릴레이 모듈이 **active-low** 인 경우가 많습니다(핀을 LOW로 내리면 ON).
> `config.yaml` 의 각 밸브 `active_high` 값으로 맞추세요.

## 전원 / 주의사항
- 밸브 솔레노이드는 별도 전원(12V/24V) + 릴레이로 구동. 파이 GPIO로 직접 구동 금지.
- I2C 장치가 많으면 주소 충돌 주의. `i2cdetect -y 1` 로 확인.
- 센서 그라운드와 파이 그라운드 공통 연결 필요.
