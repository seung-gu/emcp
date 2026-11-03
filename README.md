# emcp — e-ink 날씨 보드 서버

ESP32 e-ink 보드는 깨어날 때마다 이 서버에 상태를 보고하고, 같은 응답으로 그 보드 위치의 날씨를
받아 화면에 그린다. 사람은 Claude / ChatGPT 에서 MCP 도구로 자기 보드의 날씨 위치와 이름을 바꾸고,
`/dashboard` 에서 보고를 본다. **이 저장소는 서버만 다룬다** (ESP32 펌웨어는 별도).

```
[Claude / ChatGPT] --MCP /mcp (로그인)----->  ┌───────────┐ --보드, 보고--> Neon
[브라우저]         --/dashboard (로그인)--->  │ emcp 서버 │
[ESP32 보드]       --POST /weather (보고)-->  └───────────┘ --날씨-------> open-meteo (키 불필요)
```

로그인은 WorkOS AuthKit 이 맡고, 사람마다 자기 보드만 본다. 설계와 진행 상황은
[docs/multi-user-rollout.ko.md](docs/multi-user-rollout.ko.md) 에 있다.

## 인터페이스

### 1. MCP (Claude / ChatGPT 용)

- 전송: Streamable HTTP, 엔드포인트 `https://<host>/mcp` (공식 `mcp` Python SDK)
- 도구:
  | 도구 | 동작 |
  |------|------|
  | `register_device(code)` | 보드 디스플레이의 6자리 등록 코드로 보드를 내 계정에 등록 |
  | `get_weather_location` | 내 보드마다 번호, 이름, MAC, 날씨 위치, 역할 (`"3 / 거실 / E8F60A17AF28 / 뮌헨 (기본값) / 주인"`) |
  | `search_location(query, count=5)` | 지명으로 좌표 후보 검색 (open-meteo geocoding) |
  | `set_weather_location(device_id, name, latitude, longitude)` | 보드 하나의 날씨 위치 설정. 주인만 |
  | `rename_device(device_id, name)` | 보드 이름 바꾸기. 주인만 |

위치를 정하지 않은 보드는 뮌헨으로 조회한다. 날씨 위치를 바꾸는 정상 흐름은
**`get_weather_location` 으로 보드 번호 확인 → `search_location` → 후보 선택 →
`set_weather_location`** 이다:

```
사용자: "거실 보드 베를린 날씨로 바꿔줘"
  → get_weather_location()
      3 / 거실 / E8F60A17AF28 / 뮌헨 (기본값) / 주인
  → search_location("Berlin")
      베를린 / 베를린 / 독일 / 52.52437,13.41053 / 인구 3426354
      벌린 / 뉴햄프셔주 / 미국 / 44.46867,-71.18508 / 인구 9367   ← 동명이지 주의
  → set_weather_location(3, "베를린", 52.52437, 13.41053)
```

LLM 이 기억에 의존해 좌표를 넣으면 **틀려도 에러 없이 엉뚱한 도시 날씨가 뜬다.**
그래서 검색 단계를 도구로 노출했다. 질의는 한글보다 로마자가 정확하다 — `"광주"`
는 충남 천안의 작은 지명 하나만 나오지만 `"Gwangju"` 는 광주광역시가 최상단이다.

### 2. HTTP (ESP32 용)

| 메서드 & 경로 | 응답 | 용도 |
|---------------|------|------|
| `POST /weather`| 날씨 JSON (아래) | ESP32 가 wake 마다 호출. 등록 전에는 등록 확인 |
| `GET /health` | `ok` | Fly.io 헬스체크 |

등록 전 보드는 측정값 없이 토큰과 등록 코드만 보내 등록됐는지 묻는다. 등록 순서와 보드가 응답마다
할 일은 [docs/multi-user-rollout.ko.md](docs/multi-user-rollout.ko.md) 에 있다:

```json
{"mac": "E8F60A17AF28", "token": "<소문자 16진수 64자>", "claim_code": "4J7K2P"}
```

| 응답 | 뜻 |
|------|----|
| 200 날씨 + `"registered": true` | 등록됨. 이제부터 평소처럼 보고 |
| 200 `{"registered": false}` | 아직 아무도 코드를 입력하지 않음 |
| 409 `"new_code": true` | 다른 보드가 같은 코드를 쓰는 중. 코드를 새로 만듦 |

등록된 보드의 본문은 그 wake 의 상태 보고다. 날씨 요청에 얹혀 가므로 왕복이 늘지 않는다.
서버는 토큰으로 보드를 찾아 그 보드 위치의 날씨를 돌려준다:

```json
{"mac": "E8F60A17AF28", "token": "<토큰>", "battery_mv": 3912, "wifi_ms": 184, "rssi": -58}
```

모르는 토큰(등록 해제된 보드)에는 401 `{"registered": false}` 를 준다. 보드는 이때만 토큰을 지우고
새 코드를 띄운다. 서버가 보드를 확인하지 못한 경우는 429(횟수 제한)나 503(DB 장애)이라 토큰을 지우지
않는다.

수치는 숫자로 보내고 `°C` 나 `km/h` 는 기기가 붙인다. 바깥 날씨는 옛 펌웨어가 읽는 이름(`temp_c` 등)과
새 펌웨어가 읽는 이름(`out_temp_c` 등)을 같이 보낸다:

```json
{"city": "뮌헨", "temp_c": 19, "cond": "구름 조금", "wind_kmh": 4,
 "humidity": 61, "temp_max_c": 24, "temp_min_c": 17, "pop": 25,
 "stamp": "Fri 14:30", "out_temp_c": 19, "out_humidity": 61,
 "out_temp_max_c": 24, "out_temp_min_c": 17}
```

조회에 실패하면 날씨 키를 통째로 빼고 `{"city": "뮌헨"}` 만 남기며 **HTTP 500** 을
반환한다. 펌웨어는 상태 코드로 본문을 거르니 이 폴백은 화면에 도달하지 않는다.

## 로컬 실행

작업 디렉터리의 `.env` 에 Neon 접속 주소와 WorkOS 값을 넣어 둔다 (`.env` 는 gitignore 됨).
`MCP_AUTH_ISSUER` 가 없으면 서버가 뜨지 않는다:

```text
DATABASE_URL=postgresql://...
WORKOS_CLIENT_ID=client_...
WORKOS_API_KEY=sk_...
MCP_AUTH_ISSUER=https://<authkit 도메인>
WORKOS_COOKIE_PASSWORD=<Fernet 키>
```

```bash
uv sync
uv run --env-file .env python server.py   # 기본 포트 8080, PORT 환경변수로 변경 가능
```

확인:

```bash
curl http://127.0.0.1:8080/health                                              # ok
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8080/mcp     # 401 (로그인 필요)
```

로컬도 운영과 같은 Neon 을 쓴다. 로컬에 `POST /weather` 로 보고를 보내면 한 시간 안에 Neon 으로
옮겨져 진짜 기록에 섞인다.

## Fly.io 배포

아직 Neon 으로 옮기지 않은 보고를 담아 둘 볼륨과 DB 접속 주소가 먼저 있어야 한다. 볼륨은
`fly.toml` 의 `[mounts] source` 와 이름이 같아야 하고, 둘 다 한 번만 하면 된다:

```bash
fly volumes create reports --size 1 --region fra
fly secrets import < .env                           # 값이 화면에 찍히지 않는다
fly secrets set PUBLIC_URL=https://<app>.fly.dev    # 앱마다 다르다. 토큰의 aud 와 로그인 주소에 쓴다
fly deploy --ha=false
```

보드들이 아직 옛 주소(`emcp-led.fly.dev`)로 보고하므로, 보드를 다시 플래싱할 때까지는 옛 앱에도
같은 코드를 올린다: `fly deploy --ha=false -a emcp-led`. 옛 펌웨어는 토큰 없이 보고하므로 옛 앱에만
`ACCEPT_TOKENLESS=1` 을 준다. 이 값이 빠지면 옛 보드의 보고가 전부 거절되고, 보드는 5분마다 다시 시도하면서
배터리를 쓴다. `emcp` 에는 주지 않는다.

- MCP 엔드포인트: `https://<app>.fly.dev/mcp`
- 보드 보고: `POST https://<app>.fly.dev/weather`
- 대시보드: `https://<app>.fly.dev/dashboard`

## 보고 보관

보고는 먼저 `REPORTS_BUFFER` 가 가리키는 파일에 한 줄씩 쌓이고, 한 시간에 한 번 Neon 으로
옮겨진다. 대시보드를 열면 남은 보고를 먼저 옮긴 뒤 Neon 에서 읽는다. 보고마다 DB 에 쓰지
않는 건 Neon 무료 한도 때문이다. 테이블 구조는 [docs/data-structure.ko.md](docs/data-structure.ko.md)
에 있다. 로컬 실행은 작업 디렉터리의 `pending-reports.jsonl` 을 쓴다 (gitignore 됨).

볼륨에 파일로 쌓던 때의 기록(`/data/reports.jsonl`)은 한 번 옮긴다. 다시 돌려도 이미 들어간
보고는 건너뛴다:

```bash
fly ssh sftp get /data/reports.jsonl reports.jsonl -a emcp-led
uv run --env-file .env python -m scripts.import_reports reports.jsonl
```

`data/` 에는 볼륨을 붙이기 전, 대시보드 HTML 에서 되살린 기록이 있다:

| 파일 | 내용 |
|------|------|
| `seed-reports.jsonl` | 9/11~9/16 의 604건. 볼륨의 `reports.jsonl` 앞부분에 이미 들어 있다 |
| `reports-thru-2026-09-16.csv` | 같은 데이터를 표로 본 것 |

표에 실린 마지막 50건은 시각과 `wifi_ms` 까지 그대로고, 앞의 554건은 차트 polyline 에서
역산했다 — `y()` 가 선형이고 축 경계가 옆에 찍혀 있어서 배터리와 신호는 정확하다 (겹치는
50건이 전부 일치했다). 다만 차트 x축이 시각이 아니라 인덱스 기반이라 그 554건에는 시각이
없어서, 아는 세 지점(양 끝과 가운데 눈금) 사이를 선형으로 채우고 `"at_estimated": true`
를 달아 두었다. 보고 간격이 고르지 않으므로(재시도와 벤치 테스트가 섞여 있다) 그 시각은
순서를 세우는 용도지 믿을 값이 아니다. 차트는 x를 위치로만 쓰므로 모양은 원본 그대로다.

## 날씨 위치

보드마다 Neon 의 `devices` 에 저장한다. 보고에 DB 를 기다리지 않고 답하려고 서버가 MAC 별 사본을
메모리에 둔다. 사본은 서버가 뜰 때, 보고를 Neon 으로 옮길 때, 위치를 바꿀 때 다시 읽는다. 그래서
두 앱(`emcp`, `emcp-led`)이 같은 DB 를 쓰는 동안에는 한쪽에서 바꾼 위치가 다른 쪽에는 그 앱이 다음에
보고를 옮길 때(최대 한 시간) 들어간다.
