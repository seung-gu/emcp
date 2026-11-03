"""emcp: e-ink 날씨 보드 서버.

1. MCP (Streamable HTTP, /mcp). 로그인한 사람만 쓰고, 도구는 부른 사람의 보드만 다룬다.
   - `get_weather_location` 으로 내 보드와 각 보드의 날씨 위치를 본다.
   - `search_location` 으로 지명의 좌표 후보를 찾고, 고른 좌표를 `set_weather_location` 에 넘겨
     보드 하나의 위치를 바꾼다. `rename_device` 로 보드에 이름을 붙인다.

2. 보드용 HTTP
   - 보드는 깨어날 때마다 `POST /weather` 로 상태를 보고하고, 같은 응답으로 그 보드 위치의 날씨를 받는다.

3. 대시보드 (/dashboard). 로그인한 사람이 볼 수 있는 보드의 보고를 그린다.

보드와 보고는 Neon 에 둔다 (db.py). 공식 MCP Python SDK(`mcp` 패키지)의 FastMCP 를 사용한다.
날씨/지오코딩은 API 키가 필요 없는 open-meteo 를 쓴다.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

import auth
import dashboard
import db

# 날씨 위치는 보드마다 devices 에 있다. 보고에 DB 를 기다리지 않고 답하려고 MAC 별 사본을 메모리에 둔다.
# 사본은 서버가 뜰 때, 보고를 Neon 으로 옮길 때(DB 가 어차피 깨어 있을 때), 위치를 바꿀 때 다시 읽는다.
# 그래서 다른 앱(emcp, emcp-led)에서 바꾼 위치는 이 앱이 다음에 보고를 옮길 때 들어온다.
# 위치를 정하지 않은 보드와 아직 DB 에 없는 보드는 기본 위치로 조회한다.
_DEFAULT_LOCATION = {"name": "뮌헨", "lat": 48.14, "lon": 11.58}
_locations: dict[str | None, dict] = {}


def _load_locations() -> None:
    """DB 에서 위치 사본을 다시 읽는다. 실패하면 있던 사본을 그대로 쓴다."""
    global _locations
    try:
        _locations = db.read_locations()
    except Exception as e:
        print(f"loading locations failed, keeping the old copy: {e!r}", flush=True)

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
    _load_locations()


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


def _name(v: str) -> str | None:
    """사람이 정하는 도시 이름과 보드 이름. 앞뒤 공백을 떼고, 비었거나 40자를 넘으면 None."""
    v = v.strip()
    return v if 0 < len(v) <= 40 else None


# Fly.io 는 PORT 환경변수로 내부 포트를 넘겨준다. 없으면 8080.
_port = int(os.environ.get("PORT", "8080"))
# stateless_http: 요청마다 그 요청의 토큰으로 사용자를 정한다. 세션을 이어 쓰면 도구가 세션을 처음 연
# 사람으로 실행될 수 있다 (docs/multi-user-rollout.ko.md 2단계).
mcp = FastMCP("emcp", host="0.0.0.0", port=_port, stateless_http=True,
              **auth.fastmcp_options())

# 로그인 쿠키. HttpOnly: 페이지 스크립트가 못 읽는다. Secure: https 에서만 (로컬 http 는 예외).
# SameSite=Lax: 다른 사이트에서 보낸 POST 에는 실리지 않는다.
_COOKIE = {"httponly": True, "secure": auth.PUBLIC_URL.startswith("https://"), "samesite": "lax"}


# --- MCP 도구 (Claude/ChatGPT 가 호출) -----------------------------------------
def _caller() -> str:
    """도구를 부른 사람의 WorkOS 사용자 ID. /mcp 에는 토큰을 확인한 요청만 들어온다."""
    return get_access_token().subject


@mcp.tool()
async def get_weather_location() -> str:
    """내가 볼 수 있는 보드마다 '번호 / 이름 / MAC / 날씨 위치 / 역할' 을 한 줄씩 보여준다.
    위치나 이름을 바꿀 때 이 번호를 쓴다."""
    devices = await run_in_threadpool(db.list_devices, _caller())
    if not devices:
        return "연결된 보드가 없다"
    lines = []
    for d in devices:
        if d["city"]:
            where = f"{d['city']} ({d['lat']}, {d['lon']})"
        else:
            where = f"{_DEFAULT_LOCATION['name']} (기본값)"
        role = "주인" if d["role"] == "owner" else "공유받음"
        lines.append(f"{d['id']} / {d['name'] or '이름 없음'} / {d['mac'] or 'MAC 없음'} / {where} / {role}")
    return "\n".join(lines)


@mcp.tool()
async def set_weather_location(device_id: int, name: str, latitude: float, longitude: float) -> str:
    """보드 하나의 날씨 위치를 바꾼다. 그 보드가 다음에 깨어날 때 이 위치의 날씨를 표시한다.
    보드의 주인만 바꿀 수 있다.

    Args:
        device_id: 보드 번호 (`get_weather_location` 이 보여주는 첫 칸).
        name: 화면에 표시할 도시 이름 (예: '베를린').
        latitude: 위도 (-90 ~ 90).
        longitude: 경도 (-180 ~ 180).
    """
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return f"좌표 범위가 잘못됐다: ({latitude}, {longitude})"
    if not (city := _name(name)):
        return "도시 이름은 1~40자여야 한다"
    if not await run_in_threadpool(db.set_location, _caller(), device_id, city, latitude, longitude):
        return f"{device_id}번 보드의 주인이 아니거나 그런 보드가 없다"
    await run_in_threadpool(_load_locations)
    return f"{device_id}번 보드의 날씨 위치를 {city} ({latitude}, {longitude}) 로 설정했다"


@mcp.tool()
async def rename_device(device_id: int, name: str) -> str:
    """보드 이름을 바꾼다 (예: '거실'). 보드의 주인만 바꿀 수 있다.

    Args:
        device_id: 보드 번호 (`get_weather_location` 이 보여주는 첫 칸).
        name: 새 이름.
    """
    if not (new := _name(name)):
        return "보드 이름은 1~40자여야 한다"
    if not await run_in_threadpool(db.rename_device, _caller(), device_id, new):
        return f"{device_id}번 보드의 주인이 아니거나 그런 보드가 없다"
    return f"{device_id}번 보드 이름을 '{new}' 로 바꿨다"


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


# --- 보드와 Fly 가 부르는 HTTP 경로 ---------------------------------------------
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


async def _weather_body(loc: dict) -> tuple[dict, int, dict]:
    """loc 의 날씨 본문과 상태코드, 그리고 그 보고와 함께 DB 에 남길 날씨.

    수치는 숫자로 보낸다. `°C` 나 `km/h` 는 표시 결정이라 기기가 붙인다. 조회에
    실패하면 날씨 키를 통째로 빼고 도시만 남긴다 — 줄 단위 포맷과 달리 JSON 은
    없는 값을 자리를 채워가며 설명할 필요가 없다.

    DB 에는 반올림하기 전 값과 날씨 코드를 남긴다. 반올림과 한글 설명은 화면을 위한 것이라
    언제든 다시 만들 수 있다. 조회에 실패하면 DB 쪽은 통째로 비운다.
    """
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

    # MAC 을 안 보내는 보드는 None 키로 찾는다 (devices 의 MAC 빈 행)
    body, status, weather = await _weather_body(_locations.get(row.get("mac"), _DEFAULT_LOCATION))
    row.update(weather)
    _append_report(row)

    tail = f" +{len(row['log'])} logged" if "log" in row else ""
    if (err := row.get("room_err")) and err != "ok":
        tail += f" sht:{err}(sda{int(bool(row.get('room_sda')))}"
        tail += f" scl{int(bool(row.get('room_scl')))} ack={row.get('room_ack') or '-'})"
    print(f"report: {battery_mv}mV {wifi_ms}ms {rssi}dBm{tail}", flush=True)

    return JSONResponse(body, status_code=status, background=_flush_if_due())

@mcp.custom_route("/auth/login", methods=["GET"])
async def login(request: Request) -> RedirectResponse:
    """WorkOS 로그인 화면으로 보낸다. 로그인이 끝나면 WorkOS 가 브라우저를 /auth/callback 으로 돌려보낸다."""
    url, started = auth.start_sign_in()
    response = RedirectResponse(url)
    # state 와 PKCE 검증값은 브라우저가 돌아올 때까지 봉인한 쿠키에 맡겨 둔다
    response.set_cookie("emcp_login", started, max_age=600, **_COOKIE)
    return response


@mcp.custom_route("/auth/callback", methods=["GET"])
async def callback(request: Request):
    try:
        session = await run_in_threadpool(
            auth.finish_sign_in,
            request.cookies.get("emcp_login", ""),
            request.query_params.get("code"),
            request.query_params.get("state"),
        )
    except auth.SignInError as e:
        return HTMLResponse(str(e), status_code=403)
    response = RedirectResponse("/dashboard")
    response.set_cookie("emcp_session", session, max_age=auth.SIGN_IN_HOURS * 3600, **_COOKIE)
    response.delete_cookie("emcp_login")
    return response


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request) -> RedirectResponse:
    return RedirectResponse("/dashboard")


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_page(request: Request):
    """로그인한 사람이 볼 수 있는 보드의 보고를 훑어보는 페이지.

    ?mac=... 을 주면 그 기기만. 안 주면 전부 섞어서 보여주고 표에 mac 열이 붙는다. 남의 MAC 을 넣으면
    보고가 없는 빈 화면이 나온다 — 그 보드가 있는지조차 알려 주지 않는다.
    아직 Neon 으로 옮기지 않은 보고를 먼저 옮긴 뒤 읽는다. 그래야 방금 들어온 보고까지 보인다.
    """
    user = auth.session_user(request.cookies.get("emcp_session", ""))
    if not user:
        return RedirectResponse("/auth/login")
    _seal_buffer()
    await run_in_threadpool(_flush_sealed)
    rows = await run_in_threadpool(db.read_reports, user)
    return HTMLResponse(dashboard.render(rows, _tag(request.query_params.get("mac"))))


if __name__ == "__main__":
    # 위치 사본을 미리 채운다. 따로 스레드에서 읽으니 DB 가 느려도 서버는 바로 뜬다
    threading.Thread(target=_load_locations, daemon=True).start()
    mcp.run(transport="streamable-http")
