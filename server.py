"""Embedded LED / weather MCP server.

두 개의 인터페이스가 in-memory 공유 상태(LED flag, 날씨 위치)를 함께 본다:

1. MCP 인터페이스 (Streamable HTTP, 기본 경로 /mcp)
   - Claude/ChatGPT가 `turn_on_led` / `turn_off_led` / `get_led_status` 도구를
     호출해서 flag 를 바꾼다.
   - `search_location` 으로 지명의 좌표 후보를 찾고, 고른 좌표를
     `set_weather_location` 에 넘겨 날씨를 조회할 위치를 바꾼다.
     `get_weather_location` 으로 현재 위치를 확인한다.

2. 일반 HTTP 인터페이스 (ESP32 polling 용)
   - ESP32 는 MCP 프로토콜을 말하기 어려우므로, 단순 `GET /led` 로 현재 flag 를
     주기적으로 조회한다. 응답이 on 이면 LED 를 켜고 off 면 끈다.
   - `POST /weather` 로 상태 보고를 올리면서 같은 응답으로 날씨 JSON 을 받아 표시한다.

flag 와 위치는 프로세스 메모리에만 존재하는 휘발성 상태다. 프로세스가 재시작되면
각각 off / 뮌헨 으로 초기화된다. Fly.io 에서는 볼륨을 붙이지 않는 한 파일도 재배포
시 사라지므로, 진짜 영속이 필요하면 Fly volume + 파일/DB 로 `_led_on` 과
`_location` 저장소만 교체하면 된다.

공식 MCP Python SDK(`mcp` 패키지)의 FastMCP 를 사용한다. 날씨/지오코딩은 API 키가
필요 없는 open-meteo 를 쓴다.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

import dashboard
import db

# --- 공유 상태 (휘발성, in-memory) ---------------------------------------------
# GIL 덕분에 단일 bool 의 읽기/쓰기는 원자적이라 별도 락이 필요 없다.
_led_on = False

# 날씨를 조회할 위치. 기본값은 뮌헨이고 MCP 도구로 바꾼다.
_location = {"name": "뮌헨", "lat": 48.14, "lon": 11.58}

# 기기 상태 보고. 받으면 볼륨 위 파일에 한 줄씩 쌓고, 한 시간에 한 번 Neon 으로 옮긴다.
# 보고마다 DB 에 쓰면 30분마다 잠든 DB 를 깨워서 보드 몇 대만으로 무료 한도를 넘는다.
# 파일에 먼저 쓰니 DB 가 멈춰도 보고는 남는다. Fly 에서는 볼륨 위를 가리킨다
# (fly.toml 의 REPORTS_BUFFER).
_BUFFER = os.getenv("REPORTS_BUFFER", "pending-reports.jsonl")
_FLUSH_SECONDS = 3600
_last_flush = float("-inf")     # 뜬 뒤 첫 보고에서, 지난번에 못 옮기고 남은 파일부터 옮긴다
_flush_lock = threading.Lock()


def _append_report(row: dict) -> None:
    with open(_BUFFER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _seal_buffer() -> None:
    """지금까지 쌓인 버퍼를 봉인 파일로 넘긴다. 이벤트 루프에서만 부른다 — 보고를 덧붙이는 것도
    루프에서만 하므로, 이름을 바꾸는 사이에 한 줄이 끼어들 수 없다."""
    if os.path.exists(_BUFFER):
        os.replace(_BUFFER, f"{_BUFFER}.{time.time_ns()}")


def _flush_sealed() -> None:
    """봉인된 파일을 오래된 것부터 Neon 에 넣고 지운다. 실패하면 그 파일을 남겨 다음 번에 다시
    넣는다 — 이미 들어간 보고는 db.add_reports 가 건너뛰므로 두 번 넣어도 된다."""
    with _flush_lock:
        for path in sorted(glob.glob(glob.escape(_BUFFER) + ".*")):
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            try:
                result = db.add_reports(rows)
            except Exception as e:
                print(f"flush failed, keeping {path}: {e!r}", flush=True)
                return
            os.remove(path)
            print(f"flushed {path}: {result}", flush=True)


def _flush_if_due() -> BackgroundTask | None:
    """마지막으로 옮긴 지 한 시간이 지났으면 버퍼를 봉인하고, 응답을 보낸 뒤에 옮긴다.
    보드는 DB 를 기다리지 않는다."""
    global _last_flush
    if time.monotonic() - _last_flush < _FLUSH_SECONDS:
        return None
    _last_flush = time.monotonic()
    _seal_buffer()
    return BackgroundTask(_flush_sealed)


# 기기가 보내는 로그 항목의 종류. 대시보드가 이 문자열을 이스케이프 없이 HTML 에 넣으므로
# 화이트리스트로 거른다 — 기기가 지어낸 문자열은 페이지에 닿지 못한다.
_LOG_KINDS = {"wifi", "http", "portal-new", "portal-lost", "portal-timeout", "full"}

# 펌웨어가 DB 와 다른 이름으로 보내는 항목. 4단계에서 펌웨어를 고치면 지운다.
_BOARD_NAMES = {"fw_version": "fw"}
# 보드가 4단계부터 싣는 비밀값. 원본(report_raw)에도 남기지 않는다.
_SECRET_KEYS = ("token", "claim_code")


def _int(v) -> int | None:
    """숫자로 읽히면 int, 아니면 None. bool 은 숫자로 치지 않는다."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool | None:
    """참/거짓만 통과. 0/1 이나 "true" 는 받지 않는다 — 기기가 보내는 건 JSON 불린이고,
    여기서 느슨하게 받으면 필드가 없는 것과 false 가 섞인다."""
    return v if isinstance(v, bool) else None


def _tag(v) -> str | None:
    """펌웨어 버전처럼 기기가 만든 짧은 문자열. 대시보드가 이스케이프 없이 HTML 에 넣으므로
    태그가 될 수 있는 글자를 아예 통과시키지 않고, 길이도 자른다."""
    if not isinstance(v, str):
        return None
    v = "".join(c for c in v[:24] if c.isalnum() or c in "._-")
    return v or None


def _clean_entry(e: dict) -> dict:
    """로그 한 항목에서 서버가 아는 키만 남긴다. 종류마다 실려 오는 필드가 달라서 없는 건
    그냥 빠진다 — 자리를 채우지 않는다."""
    kind = e.get("kind")
    out = {"kind": kind if kind in _LOG_KINDS else "unknown"}
    for k in db.LOG_FIELDS:
        v = _int(e.get(k))
        if v is not None:
            out[k] = v
    return out


# db.py 의 항목 타입마다 들어온 값을 거르는 함수.
_CLEAN = {int: _int, float: _float, bool: _bool, str: _tag}


def _set_led(on: bool) -> None:
    global _led_on
    _led_on = on


# Fly.io 는 PORT 환경변수로 내부 포트를 넘겨준다. 없으면 8080.
_port = int(os.environ.get("PORT", "8080"))
mcp = FastMCP("LED Controller", host="0.0.0.0", port=_port)


# --- MCP 도구 (Claude/ChatGPT 가 호출) -----------------------------------------
@mcp.tool()
def turn_on_led() -> str:
    """LED 를 켠다. ESP32 보드가 다음 polling 때 LED 를 켠다."""
    _set_led(True)
    return "LED flag set to ON"


@mcp.tool()
def turn_off_led() -> str:
    """LED 를 끈다. ESP32 보드가 다음 polling 때 LED 를 끈다."""
    _set_led(False)
    return "LED flag set to OFF"


@mcp.tool()
def get_led_status() -> str:
    """현재 LED flag 를 반환한다 ('on' 또는 'off')."""
    return "on" if _led_on else "off"


@mcp.tool()
def set_weather_location(name: str, latitude: float, longitude: float) -> str:
    """날씨를 조회할 위치를 바꾼다. ESP32 가 다음 polling 때 이 위치의 날씨를 표시한다.

    Args:
        name: 화면에 표시할 도시 이름 (예: '베를린').
        latitude: 위도 (-90 ~ 90).
        longitude: 경도 (-180 ~ 180).
    """
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return f"좌표 범위가 잘못됐다: ({latitude}, {longitude})"
    _location.update(name=name, lat=latitude, lon=longitude)
    return f"날씨 위치를 {name} ({latitude}, {longitude}) 로 설정했다"


@mcp.tool()
def get_weather_location() -> str:
    """현재 설정된 날씨 위치를 반환한다 ('이름 (위도, 경도)')."""
    return f"{_location['name']} ({_location['lat']}, {_location['lon']})"


@mcp.tool()
async def search_location(query: str, count: int = 5) -> str:
    """지명으로 좌표 후보를 찾는다. 후보 중 하나를 골라 `set_weather_location` 에 넘긴다.

    한글보다 로마자 표기가 결과가 정확하다 (예: '광주' 대신 'Gwangju').
    후보가 여러 개면 admin1/국가/인구를 보고 판단하고, 모호하면 사용자에게 되묻는다.

    Args:
        query: 찾을 지명.
        count: 반환할 후보 개수 (1~10).
    """
    params = {"name": query, "count": max(1, min(count, 10)), "language": "ko", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://geocoding-api.open-meteo.com/v1/search", params=params)
            results = resp.json().get("results", [])
    except Exception:
        return "지오코딩 API 조회에 실패했다"
    if not results:
        return f"'{query}' 에 해당하는 지명을 찾지 못했다. 로마자 표기로 다시 시도해봐라"
    return "\n".join(
        f"{r['name']} / {r.get('admin1', '-')} / {r.get('country', '-')}"
        f" / {r['latitude']},{r['longitude']} / 인구 {r.get('population', '-')}"
        for r in results
    )


# --- ESP32 polling 용 일반 HTTP 엔드포인트 -------------------------------------
@mcp.custom_route("/led", methods=["GET"])
async def led_state(request: Request) -> PlainTextResponse:
    """ESP32 가 주기적으로 조회하는 현재 LED 상태. 본문은 1바이트: '1' = on, '0' = off."""
    return PlainTextResponse("1" if _led_on else "0")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> PlainTextResponse:
    """Fly.io 헬스체크용."""
    return PlainTextResponse("ok")


# WMO 날씨 코드 -> 한글
WMO_KO = {
    0:"맑음", 1:"대체로 맑음", 2:"구름 조금", 3:"흐림",
    45:"안개", 48:"짙은 안개",
    51:"약한 이슬비", 53:"이슬비", 55:"강한 이슬비",
    61:"약한 비", 63:"비", 65:"강한 비",
    66:"어는 비", 67:"강한 어는 비",
    71:"약한 눈", 73:"눈", 75:"강한 눈", 77:"싸락눈",
    80:"약한 소나기", 81:"소나기", 82:"강한 소나기",
    85:"약한 눈소나기", 86:"강한 눈소나기",
    95:"뇌우", 96:"뇌우(우박)", 99:"강한 뇌우(우박)",
}

WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _stamp(utc_offset_seconds: int) -> str:
    """조회 위치의 현지 시각을 'Fri 14:30' 으로. 기기가 그대로 화면에 찍는다.

    요일이 영문인 건 기기가 쓰는 작은 폰트에 한글 글리프가 없어서다. 시각을 응답의
    current.time 이 아니라 서버 시계로 만드는 건 그 값이 15분 단위로 끊겨서다.
    월/일을 뺀 건 이 줄이 화면 좌상단으로 올라가면서 도시 이름과 폭을 나눠 쓰게 돼서다.
    """
    t = datetime.now(timezone.utc) + timedelta(seconds=utc_offset_seconds)
    return f"{WEEKDAY[t.weekday()]} {t.hour:02d}:{t.minute:02d}"


async def _weather_body() -> tuple[dict, int, dict]:
    """날씨 본문과 상태코드, 그리고 그 보고와 함께 DB 에 남길 날씨.

    수치는 숫자로 보낸다. `°C` 나 `km/h` 는 표시 결정이라 기기가 붙인다. 조회에
    실패하면 날씨 키를 통째로 빼고 도시만 남긴다 — 줄 단위 포맷과 달리 JSON 은
    없는 값을 자리를 채워가며 설명할 필요가 없다.

    DB 에는 반올림하기 전 값과 날씨 코드를 남긴다. 반올림과 한글 설명은 화면을 위한 것이라
    언제든 다시 만들 수 있다. 조회에 실패하면 DB 쪽은 통째로 비운다.
    """
    loc = dict(_location)  # 요청 처리 중 위치가 바뀌어도 한 응답 안에서는 일관되게.
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&forecast_days=1&timezone=auto")  # 위치가 바뀌므로 현지 타임존을 자동으로 잡는다
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            data = (await client.get(url)).json()
        cur = data["current"]; daily = data["daily"]
        temp  = round(cur["temperature_2m"])
        desc  = WMO_KO.get(cur["weather_code"], "알수없음")
        wind  = round(cur["wind_speed_10m"])
        humid = round(cur["relative_humidity_2m"])
        tmax  = round(daily["temperature_2m_max"][0])
        tmin  = round(daily["temperature_2m_min"][0])
        pop   = daily["precipitation_probability_max"][0] or 0           # 강수확률(None 방어)
        stamp = _stamp(data.get("utc_offset_seconds", 0))
        record = {"city": loc["name"], "lat": loc["lat"], "lon": loc["lon"],
                  "out_temp_c": cur["temperature_2m"], "out_humidity": cur["relative_humidity_2m"],
                  "out_temp_max_c": daily["temperature_2m_max"][0],
                  "out_temp_min_c": daily["temperature_2m_min"][0],
                  "weather_code": cur["weather_code"], "wind_kmh": cur["wind_speed_10m"],
                  "pop": daily["precipitation_probability_max"][0]}
        return ({"city": loc["name"], "temp_c": temp, "cond": desc, "wind_kmh": wind,
                 "humidity": humid, "temp_max_c": tmax, "temp_min_c": tmin, "pop": pop,
                 "stamp": stamp}, 200, record)
    except Exception:
        # stamp 도 뺀다: 현지 시각 오프셋이 실패한 그 응답 안에 들어 있어서 서버도 모른다.
        return ({"city": loc["name"]}, 500, {})


@mcp.custom_route("/weather", methods=["GET"])
async def weather(request: Request) -> JSONResponse:
    """브라우저나 curl 로 확인할 때 쓰는 읽기 전용 경로. 기기는 POST 를 쓴다."""
    body, status, _ = await _weather_body()
    return JSONResponse(body, status_code=status)


@mcp.custom_route("/weather", methods=["POST"])
async def weather_report(request: Request) -> JSONResponse:
    """기기가 wake 마다 부르는 경로. 상태 보고를 받고 날씨를 응답한다.

    시각은 기기가 모르므로 서버 시계로 찍는다. 토큰 검증은 4단계(보드 등록)에서 들어간다.
    """
    try:
        report = await request.json()
        battery_mv, wifi_ms, rssi = (int(report[k]) for k in ("battery_mv", "wifi_ms", "rssi"))
    except Exception:
        return JSONResponse({"error": "bad body"}, status_code=400)

    row = {"received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # 어느 기기가 보냈나. 이게 없으면 여러 대가 한 줄로 섞여서, 앞 행과 비교하는 것들이
    # (배터리 추세, wake 시간) 서로 다른 기기를 비교하게 된다.
    if (mac := _tag(report.get("mac"))):
        row["mac"] = mac
    # 나머지는 있으면 싣고 없으면 만다. 기기 펌웨어와 이 서버는 따로 배포되므로 한쪽이
    # 아직 모르는 필드가 있어도 보고가 깨지면 안 된다. 항목 목록은 db.py 한 곳에 있다.
    for name, kind in {**db.REPORT_FIELDS, **db.SENSOR_FIELDS}.items():
        v = _CLEAN[kind](report.get(_BOARD_NAMES.get(name, name)))
        if v is not None:
            row[name] = v
    log = report.get("log")
    if isinstance(log, list) and log:
        row["log"] = [_clean_entry(e) for e in log if isinstance(e, dict)]
    # 받은 그대로도 남긴다. 서버가 아직 모르는 항목을 나중에 칸으로 뽑을 때 옛 보고에서도 채우려고.
    row["raw"] = {k: v for k, v in report.items() if k not in _SECRET_KEYS}

    body, status, weather = await _weather_body()
    row.update(weather)
    _append_report(row)

    tail = f" +{len(row['log'])} logged" if "log" in row else ""
    if (err := row.get("room_err")) and err != "ok":
        tail += f" sht:{err}(sda{int(bool(row.get('room_sda')))}"
        tail += f" scl{int(bool(row.get('room_scl')))} ack={row.get('room_ack') or '-'})"
    print(f"report: {battery_mv}mV {wifi_ms}ms {rssi}dBm{tail}", flush=True)

    return JSONResponse(body, status_code=status, background=_flush_if_due())

@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_page(request: Request) -> HTMLResponse:
    """기기가 보내온 보고를 훑어보는 페이지.

    ?mac=... 을 주면 그 기기만. 안 주면 전부 섞어서 보여주고 표에 mac 열이 붙는다.
    아직 Neon 으로 옮기지 않은 보고를 먼저 옮긴 뒤 읽는다. 그래야 방금 들어온 보고까지 보인다.
    """
    _seal_buffer()
    await run_in_threadpool(_flush_sealed)
    rows = await run_in_threadpool(db.read_reports)
    return HTMLResponse(dashboard.render(rows, _tag(request.query_params.get("mac"))))


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
