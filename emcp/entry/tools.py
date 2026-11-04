"""Claude/ChatGPT 가 부르는 MCP 도구. 로그인한 사람만 쓰고, 도구는 부른 사람의 보드만 다룬다.

- `register_device` 로 보드 디스플레이의 등록 코드를 입력해 보드를 등록한다.
- `get_weather_location` 으로 내 보드와 각 보드의 날씨 위치를 본다.
- `search_location` 으로 지명의 좌표 후보를 찾고, 고른 좌표를 `set_weather_location` 에 넘겨 보드 하나의 위치를
  바꾼다. `rename_device` 로 보드에 이름을 붙인다.
- `get_reports` 로 보드 하나의 최근 보고(배터리, 신호, 실내 온습도, 깨어난 이유)를 읽는다.

server.py 가 이 함수들을 도구로 등록한다. 함수 이름이 도구 이름이고 docstring 이 Claude 가 읽는 설명이다.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.concurrency import run_in_threadpool

from emcp.infra import db, weather
from emcp.service import actions, checks
from emcp.state import buffer, devices
from emcp.entry.dashboard.reports import DEFAULT_TZ, RESET_KO


def _caller() -> str:
    """도구를 부른 사람의 WorkOS 사용자 ID. /mcp 에는 토큰을 확인한 요청만 들어온다."""
    return get_access_token().subject


async def register_device(code: str) -> str:
    """보드 디스플레이에 뜬 6자리 등록 코드로 보드를 내 계정에 등록한다.

    Args:
        code: 보드 디스플레이의 등록 코드 (예: '4J7K2P').
    """
    return actions.ENTER_RESULTS[await actions.enter_code(_caller(), code)]


async def get_weather_location() -> str:
    """내가 볼 수 있는 보드마다 '번호 / 이름 / MAC / 날씨 위치 / 역할' 을 한 줄씩 보여준다.
    위치나 이름을 바꿀 때 이 번호를 쓴다."""
    boards = await run_in_threadpool(db.boards.list_devices, _caller())
    if not boards:
        return "연결된 보드가 없다"
    lines = []
    for d in boards:
        if d["city"]:
            where = f"{d['city']} ({d['lat']}, {d['lon']})"
        else:
            where = f"{devices.DEFAULT_LOCATION['name']} (기본값)"
        role = "주인" if d["role"] == "owner" else "공유받음"
        lines.append(f"{d['id']} / {d['name'] or '이름 없음'} / {d['mac'] or 'MAC 없음'} / {where} / {role}")
    return "\n".join(lines)


async def set_weather_location(device_id: int, name: str, latitude: float, longitude: float) -> str:
    """보드 하나의 날씨 위치를 바꾼다. 그 보드가 다음에 깨어날 때 이 위치의 날씨를 표시한다.
    보드의 주인만 바꿀 수 있다.

    Args:
        device_id: 보드 번호 (`get_weather_location` 이 보여주는 첫 칸).
        name: 화면에 표시할 도시 이름 (예: '베를린').
        latitude: 위도 (-90 ~ 90).
        longitude: 경도 (-180 ~ 180).
    """
    result = await actions.move_board(_caller(), device_id, name, latitude, longitude)
    if result == "bad_place":
        return f"좌표 범위가 잘못됐다: ({latitude}, {longitude})"
    if result == "bad_name":
        return "도시 이름은 1~40자여야 한다"
    if result == "not_owner":
        return f"{device_id}번 보드의 주인이 아니거나 그런 보드가 없다"
    return f"{device_id}번 보드의 날씨 위치를 {checks.name(name)} ({latitude}, {longitude}) 로 설정했다"


async def rename_device(device_id: int, name: str) -> str:
    """보드 이름을 바꾼다 (예: '거실'). 보드의 주인만 바꿀 수 있다.

    Args:
        device_id: 보드 번호 (`get_weather_location` 이 보여주는 첫 칸).
        name: 새 이름.
    """
    result = await actions.rename_board(_caller(), device_id, name)
    if result == "bad_name":
        return "보드 이름은 1~40자여야 한다"
    if result == "not_owner":
        return f"{device_id}번 보드의 주인이 아니거나 그런 보드가 없다"
    return f"{device_id}번 보드 이름을 '{checks.name(name)}' 로 바꿨다"


async def search_location(query: str, count: int = 5) -> str:
    """지명으로 좌표 후보를 찾는다. 후보 중 하나를 골라 `set_weather_location` 에 넘긴다.

    한글보다 로마자 표기가 결과가 정확하다 (예: '광주' 대신 'Gwangju').
    후보가 여러 개면 admin1/국가/인구를 보고 판단하고, 모호하면 사용자에게 되묻는다.

    Args:
        query: 찾을 지명.
        count: 반환할 후보 개수 (1~10).
    """
    results = await weather.geocode(query, count)
    if results is None:
        return "지오코딩 API 조회에 실패했다"
    if not results:
        return f"'{query}' 에 해당하는 지명을 찾지 못했다. 로마자 표기로 다시 시도해봐라"
    return "\n".join(
        f"{r['name']} / {r.get('admin1', '-')} / {r.get('country', '-')}"
        f" / {r['latitude']},{r['longitude']} / 인구 {r.get('population', '-')}"
        for r in results
    )


# get_reports 가 보여 주는 보고 줄 수. 30분마다 보고하므로 하루치다.
_REPORT_LINES = 48


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _cell(v) -> str:
    return "-" if v is None else str(v)


async def get_reports(device_id: int, hours: int = 24) -> str:
    """보드 하나의 최근 보고를 읽는다. 배터리 전압, Wi-Fi 신호와 연결 시간, 실내 온도와 습도, 평소와 다르게
    깨어난 이유(전원투입, 브라운아웃 등), 서버에 닿지 못해 나중에 실려 온 보고 수를 보여 준다. 내 보드와
    수락한 공유 보드만 볼 수 있다. 시각은 그 보드 날씨 위치의 현지 시각이다.

    Args:
        device_id: 보드 번호 (`get_weather_location` 이 보여주는 첫 칸).
        hours: 몇 시간 전 보고부터 볼지 (1~168, 기본 24).
    """
    hours = max(1, min(hours, 168))
    user = _caller()
    boards = await run_in_threadpool(db.boards.list_devices, user)
    board = next((b for b in boards if b["id"] == device_id), None)
    if board is None:
        return f"{device_id}번 보드는 볼 수 없다 (내 보드나 수락한 공유 보드가 아니거나 없는 번호)"
    # 대시보드처럼, 버퍼에만 있는 방금 들어온 보고까지 Neon 으로 옮긴 뒤 읽는다
    buffer.seal()
    await run_in_threadpool(actions.flush_reports)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await run_in_threadpool(db.reports.recent_reports, user, device_id, since) or []
    name = board["name"] or board["mac"] or f"{device_id}번 보드"
    if not rows:
        return f"{name}: 최근 {hours}시간 동안 보고가 없다"

    zone = _zone(rows[-1]["timezone"])
    last = rows[-1]
    batteries = [r["battery_mv"] for r in rows if r["battery_mv"] is not None]
    unusual = [RESET_KO.get(r["reset_reason"], f"코드 {r['reset_reason']}") for r in rows
               if r["reset_reason"] is not None and r["reset_reason"] != 8]   # 8 은 타이머로 정상 기상
    lines = [
        f"{name} ({board['mac'] or 'MAC 없음'}) / 최근 {hours}시간 보고 {len(rows)}건 / 시각은 {zone.key}",
        f"마지막 보고 {last['received_at'].astimezone(zone):%m-%d %H:%M}, 배터리 {_cell(last['battery_mv'])} mV"
        + (f" (기간 중 최저 {min(batteries)}, 최고 {max(batteries)})" if batteries else "")
        + f", 신호 {_cell(last['rssi'])} dBm, 실내 {_cell(last['room_c'])}°C {_cell(last['room_rh'])}%,"
        f" 펌웨어 {_cell(last['fw_version'])}",
        f"평소와 다른 깨어남 {len(unusual)}건" + (f" ({', '.join(unusual)})" if unusual else "")
        + f", 못 보낸 보고 {sum(r['logged'] for r in rows)}건",
        "",
        "시각 / 배터리 mV / 신호 dBm / Wi-Fi ms / 실내 °C / 습도 % / 깨어난 이유",
    ]
    shown = rows[-_REPORT_LINES:]
    if len(shown) < len(rows):
        lines.append(f"(가장 최근 {len(shown)}건만)")
    for r in reversed(shown):
        reason = "" if r["reset_reason"] in (None, 8) else RESET_KO.get(r["reset_reason"], f"코드 {r['reset_reason']}")
        lines.append(f"{r['received_at'].astimezone(zone):%m-%d %H:%M} / {_cell(r['battery_mv'])} / {_cell(r['rssi'])}"
                     f" / {_cell(r['wifi_ms'])} / {_cell(r['room_c'])} / {_cell(r['room_rh'])} / {reason}")
    return "\n".join(lines)
