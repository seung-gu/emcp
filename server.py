"""emcp: e-ink 날씨 보드 서버.

1. MCP (Streamable HTTP, /mcp). 로그인한 사람만 쓰고, 도구는 부른 사람의 보드만 다룬다.
   - `register_device` 로 보드 디스플레이의 등록 코드를 입력해 보드를 등록한다.
   - `get_weather_location` 으로 내 보드와 각 보드의 날씨 위치를 본다.
   - `search_location` 으로 지명의 좌표 후보를 찾고, 고른 좌표를 `set_weather_location` 에 넘겨
     보드 하나의 위치를 바꾼다. `rename_device` 로 보드에 이름을 붙인다.

2. 보드용 HTTP (`POST /weather`)
   - 등록 전 보드는 토큰과 등록 코드만 보내 등록됐는지 묻는다.
   - 등록된 보드는 깨어날 때마다 토큰을 실어 상태를 보고하고, 같은 응답으로 그 보드 위치의 날씨를 받는다.

3. 대시보드 (/dashboard). 로그인한 사람이 볼 수 있는 보드의 보고를 그리고, 보드를 등록하거나 등록을
   해제한다.

보드 등록 설계는 docs/multi-user-rollout.ko.md 에 있다.

보드와 보고는 Neon 에 둔다 (db.py). 공식 MCP Python SDK(`mcp` 패키지)의 FastMCP 를 사용한다.
날씨/지오코딩은 API 키가 필요 없는 open-meteo 를 쓴다.
"""

from __future__ import annotations

import glob
import json
import os
import re
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

# 보드 사본. 보고에 DB 를 기다리지 않고 답하려고 devices 를 통째로 메모리에 둔다. 토큰으로 온 보고는
# 토큰 해시로, 토큰 없는 옛 보고는 MAC 으로 보드를 찾는다. 사본은 서버가 뜰 때, 보고를 Neon 으로 옮길 때
# (DB 가 어차피 깨어 있을 때), 위치를 바꾸거나 보드를 등록·해제할 때 다시 읽는다. 그래서 다른 앱(emcp,
# emcp-led)에서 바꾼 위치는 이 앱이 다음에 보고를 옮길 때 들어온다.
# 위치를 정하지 않은 보드와 아직 DB 에 없는 보드는 기본 위치로 조회한다.
_DEFAULT_LOCATION = {"name": "뮌헨", "lat": 48.14, "lon": 11.58}
_by_token: dict[str, dict] = {}
_by_mac: dict[str | None, dict] = {}


def _load_devices() -> bool:
    """DB 에서 보드 사본을 다시 읽는다. 실패하면 있던 사본을 그대로 두고 False 를 돌려준다."""
    global _by_token, _by_mac
    try:
        rows = db.read_devices()
    except Exception as e:
        print(f"loading devices failed, keeping the old copy: {e!r}", flush=True)
        return False
    _by_token = {r["token_hash"]: r for r in rows if r["token_hash"]}
    # 같은 MAC 의 행이 여럿이면 가장 오래된 행. 토큰 없는 옛 보고를 db._device_ids 와 같은 행으로 보낸다.
    _by_mac = {r["mac"]: r for r in sorted(rows, key=lambda r: r["id"], reverse=True)}
    return True


def _location(device: dict | None) -> dict:
    if device and device["city"]:
        return {"name": device["city"], "lat": device["lat"], "lon": device["lon"]}
    return _DEFAULT_LOCATION

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
    _forget_deleted_users()
    _load_devices()


# WorkOS 에서 지워진 계정의 보드를 해제한다. WorkOS 는 계정이 지워져도 이 서버에 알리지 않으므로, 보고를
# 옮기는 김에(DB 가 어차피 깨어 있을 때) 그 뒤로 지워진 계정을 물어본다. 대시보드를 열 때도 보고를 옮기니
# 매번 WorkOS 를 부르지 않게 30분에 한 번만 묻는다. 어디까지 물어봤는지는 DB 에 둬서 재시작과 두 앱 사이에도
# 이어진다. 처음 켜질 때는 그 전의 삭제를 건너뛰고 그때부터 센다.
_ACCOUNT_CHECK_SECONDS = 1800
_last_account_check = float("-inf")


def _forget_deleted_users() -> None:
    global _last_account_check
    if time.monotonic() - _last_account_check < _ACCOUNT_CHECK_SECONDS:
        return
    _last_account_check = time.monotonic()
    try:
        since = db.get_state("deleted_users_since")
        if since is None:
            db.set_state("deleted_users_since", datetime.now(timezone.utc).isoformat())
            return
        for deleted_at, user_id in auth.deleted_users_since(datetime.fromisoformat(since)):
            released = db.forget_user(user_id)
            print(f"account deleted: {user_id}, released boards {released}", flush=True)
            # 1ms 뒤부터 묻는다. WorkOS 는 시작 시각을 포함해서 돌려주므로 같은 삭제를 또 받지 않게.
            db.set_state("deleted_users_since", (deleted_at + timedelta(milliseconds=1)).isoformat())
    except Exception as e:
        print(f"checking deleted accounts failed: {e!r}", flush=True)


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

# 옛 펌웨어가 DB 와 다른 이름으로 보내는 항목. 4단계 펌웨어는 새 이름으로 보내지만, 옛 펌웨어 보드가 남아
# 있는 동안 둘 다 받는다.
_BOARD_NAMES = {"fw_version": "fw"}
# 보드가 4단계부터 싣는 비밀값. 원본(report_raw)에도 남기지 않는다.
_SECRET_KEYS = ("token", "claim_code")

# 옛 펌웨어 보드는 토큰 없이 MAC 만 보낸다. 세 보드를 다시 플래싱할 때까지, 그 보드들이 보고하는 emcp-led
# 에만 ACCEPT_TOKENLESS=1 을 준다. 옛 펌웨어는 거절당하면 5분마다 다시 시도해서 배터리가 빨리 닳는다.
_ACCEPT_TOKENLESS = os.getenv("ACCEPT_TOKENLESS") == "1"

# 보드가 만드는 값의 모양. 펌웨어와 맞춘 약속이다. 토큰은 32바이트 난수를 소문자 16진수로 쓴 64자,
# 등록 코드는 헷갈리는 0, 1, I, O 를 뺀 32글자 중 6자리다.
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# 요청 횟수 제한 (1시간에). 보드 하나는 등록 전 1시간에 12번 묻는다.
_CHECK_INS_PER_IP = 30   # 등록 확인. 한 집에서 두 대를 같이 등록해도 넉넉하게
_LOOKUPS_PER_IP = 12     # 사본에 없는 토큰 때문에 DB 를 다시 읽는 횟수
_CODES_PER_USER = 10     # 사람이 코드를 입력하는 횟수. 코드를 찍어서 맞히지 못하게
_hits: dict[str, list[float]] = {}


def _allowed(key: str, limit: int, per: int = 3600) -> bool:
    """key 로 per 초 안에 limit 번까지 허용한다. 메모리에만 세므로 재시작하면 처음부터 센다. 키가 지워지지
    않고 쌓이지만 보드와 사람 수가 적어서 그대로 둔다. 많아지면 오래된 키를 주기적으로 비운다."""
    now = time.monotonic()
    recent = [t for t in _hits.get(key, ()) if now - t < per]
    allowed = len(recent) < limit
    if allowed:
        recent.append(now)
    _hits[key] = recent
    return allowed


def _token(v) -> str | None:
    return v if isinstance(v, str) and _TOKEN_RE.fullmatch(v) else None


def _code(v) -> str | None:
    """등록 코드. 사람이 넣은 대소문자, 공백, 하이픈은 무시한다."""
    if not isinstance(v, str):
        return None
    v = "".join(v.split()).replace("-", "").upper()
    return v if len(v) == 6 and all(c in _CODE_CHARS for c in v) else None


def _field(report: dict, name: str):
    """보고에서 name 항목. 없으면 옛 펌웨어가 쓰던 이름으로 찾는다."""
    v = report.get(name)
    if v is None and name in _BOARD_NAMES:
        v = report.get(_BOARD_NAMES[name])
    return v


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
    for k, kind in db.LOG_FIELDS.items():
        v = _CLEAN[kind](e.get(k))
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


# 코드를 입력한 결과. 대시보드 안내와 Claude 답에 같이 쓴다.
_ENTER_RESULTS = {
    "registered": "보드를 등록했습니다.",
    "pending": "코드를 받았습니다. 보드의 RESET 버튼을 한 번 누르면 바로 등록됩니다.",
    "taken": "이미 다른 계정이 입력한 코드입니다. 보드 디스플레이의 코드를 다시 확인해 주세요.",
    "invalid": "등록 코드는 보드 디스플레이에 나온 6자리입니다.",
    "limited": "코드를 너무 많이 입력했습니다. 1시간 뒤에 다시 해 주세요.",
    "error": "서버가 잠시 DB에 닿지 못했습니다. 잠시 뒤에 다시 해 주세요.",
}


async def _enter_code(user: str, raw) -> str:
    """대시보드와 Claude 가 같이 쓰는 코드 입력. _ENTER_RESULTS 의 키를 돌려준다."""
    if not (code := _code(raw)):
        return "invalid"
    if not _allowed(f"code:{user}", _CODES_PER_USER):
        return "limited"
    try:
        email = await run_in_threadpool(auth.user_email, user)
        result, _ = await run_in_threadpool(db.enter_code, user, code, email)
    except Exception as e:
        print(f"entering a code failed: {e!r}", flush=True)
        return "error"
    if result == "registered":
        await run_in_threadpool(_load_devices)
    return result


@mcp.tool()
async def register_device(code: str) -> str:
    """보드 디스플레이에 뜬 6자리 등록 코드로 보드를 내 계정에 등록한다.

    Args:
        code: 보드 디스플레이의 등록 코드 (예: '4J7K2P').
    """
    return _ENTER_RESULTS[await _enter_code(_caller(), code)]


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
    await run_in_threadpool(_load_devices)
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
                 "stamp": stamp,
                 # 4단계 펌웨어가 읽는 새 이름. 옛 펌웨어 보드가 남아 있는 동안 옛 이름과 같이 보낸다
                 "out_temp_c": temp, "out_humidity": humid, "out_temp_max_c": tmax,
                 "out_temp_min_c": tmin}, 200, record)
    except Exception:
        # stamp 도 뺀다: 현지 시각 오프셋이 실패한 그 응답 안에 들어 있어서 서버도 모른다.
        return ({"city": loc["name"]}, 500, {})


@mcp.custom_route("/weather", methods=["POST"])
async def weather_report(request: Request) -> JSONResponse:
    """보드가 부르는 경로. 등록 전 보드의 등록 확인과 등록된 보드의 상태 보고를 받고 날씨를 응답한다.

    보드는 401 과 {"registered": false} 를 받을 때만 토큰을 지우고 새 코드를 띄운다. 그래서 서버가 보드를
    확인하지 못한 경우(DB 장애, 횟수 제한)에는 401 이 아닌 503, 429 로 답한다.
    """
    try:
        report = await request.json()
        if not isinstance(report, dict):
            raise ValueError("not an object")
    except Exception:
        return JSONResponse({"error": "bad body"}, status_code=400)
    ip = request.client.host if request.client else "unknown"
    mac = _tag(report.get("mac"))
    if "claim_code" in report:
        return await _check_in(report, mac, ip)

    if "token" in report:
        device = _by_token.get(token_hash := db.hash_token(str(report["token"])))
        if device is None:
            # 사본에 없으면 DB 를 한 번 다시 읽는다. 모르는 토큰을 쏟아부어 DB 를 계속 깨우지 못하게 IP 마다
            # 횟수를 묶는다.
            if not _allowed(f"lookup:{ip}", _LOOKUPS_PER_IP):
                return JSONResponse({"error": "too many requests"}, status_code=429)
            if not await run_in_threadpool(_load_devices):
                return JSONResponse({"error": "try again later"}, status_code=503)
            device = _by_token.get(token_hash)
        if device is None:
            return JSONResponse({"registered": False}, status_code=401)
    elif _ACCEPT_TOKENLESS:
        device = _by_mac.get(mac)   # MAC 을 안 보내는 옛 보드는 None 키로 찾는다 (devices 의 MAC 빈 행)
    else:
        return JSONResponse({"registered": False}, status_code=401)

    try:
        battery_mv, wifi_ms, rssi = (int(report[k]) for k in ("battery_mv", "wifi_ms", "rssi"))
    except Exception:
        return JSONResponse({"error": "bad body"}, status_code=400)

    row = {"received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # 토큰으로 온 보고는 보드 번호를 적어 두고, 옛 보고는 Neon 으로 옮길 때 MAC 으로 보드를 찾는다.
    if "token" in report:
        row["device_id"] = device["id"]
    if mac:
        row["mac"] = mac
    # 나머지는 있으면 싣고 없으면 만다. 보드 펌웨어와 이 서버는 따로 배포되므로 한쪽이
    # 아직 모르는 필드가 있어도 보고가 깨지면 안 된다. 항목 목록은 db.py 한 곳에 있다.
    for name, kind in {**db.REPORT_FIELDS, **db.SENSOR_FIELDS}.items():
        v = _CLEAN[kind](_field(report, name))
        if v is not None:
            row[name] = v
    log = report.get("log")
    if isinstance(log, list) and log:
        row["log"] = [_clean_entry(e) for e in log if isinstance(e, dict)]
    # 받은 그대로도 남긴다. 서버가 아직 모르는 항목을 나중에 칸으로 뽑을 때 옛 보고에서도 채우려고.
    row["raw"] = {k: v for k, v in report.items() if k not in _SECRET_KEYS}

    body, status, weather = await _weather_body(_location(device))
    row.update(weather)
    _append_report(row)

    tail = f" +{len(row['log'])} logged" if "log" in row else ""
    if (err := row.get("room_err")) and err != "ok":
        tail += f" sht:{err}(sda{int(bool(row.get('room_sda')))}"
        tail += f" scl{int(bool(row.get('room_scl')))} ack={row.get('room_ack') or '-'})"
    print(f"report: {battery_mv}mV {wifi_ms}ms {rssi}dBm{tail}", flush=True)

    return JSONResponse(body, status_code=status, background=_flush_if_due())


async def _check_in(report: dict, mac: str | None, ip: str) -> JSONResponse:
    """등록 전 보드가 등록됐는지 묻는다. 측정값은 싣지 않으므로 보고로 저장하지 않고, 등록되면 그때부터
    날씨를 준다. 이 경로만 DB 를 바로 쓰는데, 보드 하나가 등록하는 1시간 동안만 묻는다."""
    token, code = _token(report.get("token")), _code(report.get("claim_code"))
    if not (token and code and mac):
        return JSONResponse({"error": "bad body"}, status_code=400)
    if not _allowed(f"check-in:{ip}", _CHECK_INS_PER_IP):
        return JSONResponse({"error": "too many requests"}, status_code=429)
    try:
        result, _ = await run_in_threadpool(db.board_check_in, db.hash_token(token), mac, code)
    except Exception as e:
        print(f"check-in failed: {e!r}", flush=True)
        return JSONResponse({"error": "try again later"}, status_code=503)
    if result == "taken":
        # 다른 보드가 같은 코드를 쓰고 있다. 보드는 코드를 새로 만들어 띄운다.
        return JSONResponse({"registered": False, "new_code": True}, status_code=409)
    if result != "registered":
        return JSONResponse({"registered": False})
    await run_in_threadpool(_load_devices)
    body, status, _ = await _weather_body(_location(_by_token.get(db.hash_token(token))))
    print(f"registered: {mac}", flush=True)
    return JSONResponse({**body, "registered": True}, status_code=status)


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


# 대시보드 위쪽에 띄우는 안내. 주소의 ?notice= 는 이 키만 받으므로 다른 글자가 페이지에 들어가지 않는다.
_NOTICES = {
    **_ENTER_RESULTS,
    "released": "등록을 해제했습니다. 보드는 다음 보고 때 새 코드를 띄웁니다. 지난 기록은 계속 볼 수 있습니다.",
    "not_released": "내가 주인인 등록된 보드만 등록을 해제할 수 있습니다.",
    "prefilled": "보드 디스플레이의 코드를 채워 두었습니다. '보드 등록'을 누르세요.",
    "imported": "예전 기록을 이 보드로 가져왔습니다.",
    "kept": "예전 기록은 가져오지 않고 그대로 두었습니다. 다시 묻지 않습니다.",
    "not_imported": "가져올 예전 기록이 없거나 내가 주인인 보드가 아닙니다.",
}


def _signed_in(request: Request) -> str | None:
    return auth.session_user(request.cookies.get("emcp_session", ""))


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard_page(request: Request):
    """로그인한 사람이 볼 수 있는 보드의 보고를 훑어보고, 보드를 등록하거나 등록을 해제하는 페이지.

    ?mac=... 을 주면 그 보드만. 안 주면 전부 섞어서 보여주고 표에 보드 열이 붙는다. 남의 MAC 을 넣으면
    보고가 없는 빈 화면이 나온다. 그 보드가 있는지조차 알려 주지 않는다.
    아직 Neon 으로 옮기지 않은 보고를 먼저 옮긴 뒤 읽는다. 그래야 방금 들어온 보고까지 보인다.
    """
    user = _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login")
    _seal_buffer()
    await run_in_threadpool(_flush_sealed)
    rows = await run_in_threadpool(db.read_reports, user)
    boards = await run_in_threadpool(db.list_devices, user)
    pending = await run_in_threadpool(db.pending_codes, user)
    history = await run_in_threadpool(db.previous_history, user)
    # QR 로 왔으면 코드를 입력 칸에 채운다. 로그인을 거쳐 왔으면 /r 이 쿠키에 맡겨 둔 코드다.
    prefill = _code(request.query_params.get("code")) or _code(request.cookies.get("emcp_claim"))
    notice = _NOTICES.get(request.query_params.get("notice", "")) or (prefill and _NOTICES["prefilled"])
    response = HTMLResponse(dashboard.render(rows, _tag(request.query_params.get("mac")), boards=boards,
                                             pending=pending, notice=notice, prefill=prefill, history=history))
    if "emcp_claim" in request.cookies:
        response.delete_cookie("emcp_claim")
    return response


@mcp.custom_route("/dashboard/claim", methods=["POST"])
async def dashboard_claim(request: Request) -> RedirectResponse:
    """대시보드에서 보드 디스플레이의 등록 코드를 입력한다. 로그인 쿠키는 SameSite=Lax 라 다른 사이트가
    보낸 POST 에는 실리지 않는다."""
    user = _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    result = await _enter_code(user, form.get("code"))
    return RedirectResponse(f"/dashboard?notice={result}", status_code=303)


@mcp.custom_route("/dashboard/release", methods=["POST"])
async def dashboard_release(request: Request) -> RedirectResponse:
    """보드의 등록을 해제한다. 보드를 남에게 넘길 때 전 주인이 누른다."""
    user = _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = _int(form.get("device_id"))
    released = device_id is not None and await run_in_threadpool(db.release_device, user, device_id)
    if released:
        await run_in_threadpool(_load_devices)
    return RedirectResponse(f"/dashboard?notice={'released' if released else 'not_released'}", status_code=303)


@mcp.custom_route("/dashboard/history", methods=["POST"])
async def dashboard_history(request: Request) -> RedirectResponse:
    """등록된 보드에 예전 기록이 있을 때 "가져오기"(choice=import) 또는 "그냥 두기"(choice=keep). 예전 기록은
    주인 이메일이 같은 경우에만 대시보드에 뜬다 (db.previous_history)."""
    user = _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = _int(form.get("device_id"))
    if device_id is None:
        return RedirectResponse("/dashboard?notice=not_imported", status_code=303)
    if form.get("choice") == "import":
        done = await run_in_threadpool(db.import_history, user, device_id)
        notice = "imported" if done else "not_imported"
    else:
        done = await run_in_threadpool(db.decline_history, user, device_id)
        notice = "kept" if done else "not_imported"
    return RedirectResponse(f"/dashboard?notice={notice}", status_code=303)


@mcp.custom_route("/r/{code}", methods=["GET"])
async def scanned(request: Request) -> RedirectResponse:
    """보드 디스플레이의 QR 이 여는 주소. 코드가 채워진 대시보드로 보낸다. 로그인이 안 돼 있으면 로그인을
    거친 뒤에도 코드가 남도록 쿠키에 10분 맡겨 둔다.

    여는 것만으로 등록하지는 않는다. 남이 보낸 링크를 눌렀다고 그 사람의 보드가 내 계정에 붙으면 안 되니,
    대시보드에서 '보드 등록' 을 눌러야 등록된다."""
    code = _code(request.path_params.get("code"))
    if not code:
        return RedirectResponse("/dashboard")
    if _signed_in(request):
        return RedirectResponse(f"/dashboard?code={code}")
    response = RedirectResponse("/auth/login")
    response.set_cookie("emcp_claim", code, max_age=600, **_COOKIE)
    return response


if __name__ == "__main__":
    # 보드 사본을 미리 채운다. 따로 스레드에서 읽으니 DB 가 느려도 서버는 바로 뜬다
    threading.Thread(target=_load_devices, daemon=True).start()
    mcp.run(transport="streamable-http")
