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
   /weather 응답을 화면에 표시                   open-meteo (키 불필요)
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
  | `get_weather_location` | 현재 위치 반환 (`"뮌헨 (48.14, 11.58)"`) |

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
| `GET /weather`| 날씨 JSON (아래) | 브라우저·curl 확인용 |
| `POST /weather`| 날씨 JSON (아래) | ESP32 가 wake 마다 호출 |
| `GET /health` | `ok` | Fly.io 헬스체크 |

ESP32 는 `GET /led` 를 일정 주기로 호출해서 본문이 `1` 이면 LED 를 켜고 `0` 이면
끄면 된다 (파싱 없이 첫 바이트만 비교).

`POST /weather` 의 본문은 그 wake 의 상태 보고다. 날씨 요청에 얹혀 가므로 왕복이
늘지 않는다:

```json
{"battery_mv": 3912, "wifi_ms": 184, "rssi": -58}
```

응답은 두 경로가 같다. 수치는 숫자로 보내고 `°C` 나 `km/h` 는 기기가 붙인다:

```json
{"city": "뮌헨", "temp_c": 19, "cond": "구름 조금", "wind_kmh": 4,
 "humidity": 61, "temp_max_c": 24, "temp_min_c": 17, "pop": 25,
 "stamp": "9/11(Fri) 14:30"}
```

조회에 실패하면 날씨 키를 통째로 빼고 `{"city": "뮌헨"}` 만 남기며 **HTTP 500** 을
반환한다. 펌웨어는 상태 코드로 본문을 거르니 이 폴백은 화면에 도달하지 않는다 —
curl 로 볼 때만 보인다.

## 로컬 실행

```bash
uv sync
uv run python server.py        # 기본 포트 8080, PORT 환경변수로 변경 가능
```

확인:

```bash
curl http://127.0.0.1:8080/led      # 0
curl http://127.0.0.1:8080/health   # ok
curl -s http://127.0.0.1:8080/weather | jq   # 날씨 JSON
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
