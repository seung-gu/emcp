"""Claude/ChatGPT 가 부르는 MCP 도구. 로그인한 사람만 쓰고, 도구는 부른 사람의 보드만 다룬다.

- `register_device` 로 보드 디스플레이의 등록 코드를 입력해 보드를 등록한다.
- `get_weather_location` 으로 내 보드와 각 보드의 날씨 위치를 본다.
- `search_location` 으로 지명의 좌표 후보를 찾고, 고른 좌표를 `set_weather_location` 에 넘겨 보드 하나의 위치를
  바꾼다. `rename_device` 로 보드에 이름을 붙인다.

server.py 가 이 함수들을 도구로 등록한다. 함수 이름이 도구 이름이고 docstring 이 Claude 가 읽는 설명이다.
"""

from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.concurrency import run_in_threadpool

import actions
import checks
import db.boards
import devices
import weather


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
