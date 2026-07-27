# emcp — Embedded LED / Weather MCP Server

Claude / ChatGPT 가 MCP 도구로 LED flag 를 켜고/끄거나 날씨 위치를 바꾸면, ESP32
보드가 이 서버를 polling 해서 LED 와 화면에 반영하는 구조. **이 저장소는 MCP
서버만 다룬다** (ESP32 펌웨어는 별도).

```
[Claude / ChatGPT]  --MCP (Streamable HTTP)-->  ┌────────────────────────┐
                                                │  emcp 서버              │
                                                │  flag:     on / off     │
[ESP32 board]  ----GET /led     (polling)---->  │  location: 뮌헨 48,11   │
               ----GET /weather (polling)---->  └────────────────────────┘
   on 이면 LED ON, off 면 LED OFF                        │
   /weather 7줄을 화면에 표시                    open-meteo (키 불필요)
```

두 인터페이스는 **in-memory 공유 상태** 를 함께 본다.

## 인터페이스

### 1. MCP (Claude / ChatGPT 용)

- 전송: Streamable HTTP, 엔드포인트 `https://<host>/mcp` (공식 `mcp` Python SDK)
- 도구:
  | 도구 | 동작 |
  |------|------|
  | `turn_on_led`   | flag 를 on 으로 설정 |
  | `turn_off_led`  | flag 를 off 로 설정 |
  | `get_led_status`| 현재 flag 반환 (`"on"` / `"off"`) |
  | `search_location(query, count=5)` | 지명으로 좌표 후보 검색 (open-meteo geocoding) |
  | `set_weather_location(name, latitude, longitude)` | 날씨를 조회할 위치 설정 |

날씨 위치를 바꾸는 정상 흐름은 **`search_location` → 후보 선택 →
`set_weather_location`** 이다:

```
사용자: "베를린 날씨 보여줘"
  → search_location("Berlin")
      베를린 / 베를린 / 독일 / 52.52437,13.41053 / 인구 3426354
      벌린 / 뉴햄프셔주 / 미국 / 44.46867,-71.18508 / 인구 9367   ← 동명이지 주의
  → set_weather_location("베를린", 52.52437, 13.41053)
```

LLM 이 기억에 의존해 좌표를 넣으면 **틀려도 에러 없이 엉뚱한 도시 날씨가 뜬다.**
그래서 검색 단계를 도구로 노출했다. 질의는 한글보다 로마자가 정확하다 — `"광주"`
는 충남 천안의 작은 지명 하나만 나오지만 `"Gwangju"` 는 광주광역시가 최상단이다.

### 2. HTTP polling (ESP32 용)

| 메서드 & 경로 | 응답 | 용도 |
|---------------|------|------|
| `GET /led`    | `1` (on) 또는 `0` (off) — 1바이트 plain text | ESP32 가 주기적으로 조회 |
| `GET /weather`| 7줄 plain text (아래) | ESP32 화면 표시 |
| `GET /health` | `ok` | Fly.io 헬스체크 |

ESP32 는 `GET /led` 를 일정 주기로 호출해서 본문이 `1` 이면 LED 를 켜고 `0` 이면
끄면 된다 (파싱 없이 첫 바이트만 비교).

`GET /weather` 는 항상 **7줄 고정**이다:

```
뮌헨            ← 도시 이름
19°C            ← 현재 기온
구름 조금        ← WMO 코드 -> 한글
4km/h           ← 풍속
61%             ← 습도
24°/17°         ← 오늘 최고/최저
25%             ← 강수확률
```

조회 실패 시에도 줄 수는 7줄로 유지되지만 값이 `--` 이고 **HTTP 500** 을 반환한다.
펌웨어가 상태 코드로 본문을 거르면 폴백이 표시되지 않으니 주의.

## 로컬 실행

```bash
uv sync
uv run python server.py        # 기본 포트 8080, PORT 환경변수로 변경 가능
```

확인:

```bash
curl http://127.0.0.1:8080/led      # 0
curl http://127.0.0.1:8080/health   # ok
curl http://127.0.0.1:8080/weather  # 뮌헨 / 기온 / 상태 / 풍속 / 습도 / 최고최저 / 강수확률
```

## Fly.io 배포

```bash
fly launch --no-deploy   # 또는 기존 fly.toml 사용
fly deploy
```

- MCP 엔드포인트: `https://<app>.fly.dev/mcp`
- ESP32 polling: `https://<app>.fly.dev/led`, `https://<app>.fly.dev/weather`

## 상태에 대한 주의

LED flag 와 날씨 위치는 **프로세스 메모리에만 존재하는 휘발성 상태**다. 재시작하면
각각 `off` / `뮌헨` 으로 초기화된다. 그래서 `fly.toml` 에서
`auto_stop_machines = "off"`, `min_machines_running = 1` 로 두어 머신이 꺼지지
않게 했다. **단, 재배포하면 초기화된다.**

재배포/재시작 후에도 값을 유지하려면 Fly volume 을 붙여 `server.py` 의
`_led_on` / `_location` 저장소만 파일 또는 DB 로 교체하면 된다.
