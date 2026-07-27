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
   - `GET /weather` 로 현재 설정된 위치의 날씨를 7줄 plain text 로 받아 표시한다.

flag 와 위치는 프로세스 메모리에만 존재하는 휘발성 상태다. 프로세스가 재시작되면
각각 off / 뮌헨 으로 초기화된다. Fly.io 에서는 볼륨을 붙이지 않는 한 파일도 재배포
시 사라지므로, 진짜 영속이 필요하면 Fly volume + 파일/DB 로 `_led_on` 과
`_location` 저장소만 교체하면 된다.

공식 MCP Python SDK(`mcp` 패키지)의 FastMCP 를 사용한다. 날씨/지오코딩은 API 키가
필요 없는 open-meteo 를 쓴다.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# --- 공유 상태 (휘발성, in-memory) ---------------------------------------------
# GIL 덕분에 단일 bool 의 읽기/쓰기는 원자적이라 별도 락이 필요 없다.
_led_on = False

# 날씨를 조회할 위치. 기본값은 뮌헨이고 MCP 도구로 바꾼다.
_location = {"name": "뮌헨", "lat": 48.14, "lon": 11.58}


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

@mcp.custom_route("/weather", methods=["GET"])
async def weather(request: Request) -> PlainTextResponse:
    """현재 설정된 위치의 날씨. 7줄: 도시/기온/상태/바람/습도/최고°최저°/강수%."""
    loc = dict(_location)  # 요청 처리 중 위치가 바뀌어도 한 응답 안에서는 일관되게.
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={loc['lat']}&longitude={loc['lon']}"
           "&current=temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"  # ← 추가
           "&forecast_days=1&timezone=auto")  # 위치가 바뀌므로 현지 타임존을 자동으로 잡는다
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            data = (await client.get(url)).json()
        cur = data["current"]; daily = data["daily"]
        temp  = round(cur["temperature_2m"])
        desc  = WMO_KO.get(cur["weather_code"], "알수없음")
        wind  = round(cur["wind_speed_10m"])
        humid = round(cur["relative_humidity_2m"])
        tmax  = round(daily["temperature_2m_max"][0])                    # ← 최고
        tmin  = round(daily["temperature_2m_min"][0])                    # ← 최저
        pop   = daily["precipitation_probability_max"][0] or 0           # ← 강수확률(None 방어)
        return PlainTextResponse(
            f"{loc['name']}\n{temp}°C\n{desc}\n{wind}km/h\n{humid}%\n{tmax}°/{tmin}°\n{pop}%")  # ← 2줄 추가
    except Exception:
        return PlainTextResponse(
            f"{loc['name']}\n--°C\n조회실패\n--km/h\n--%\n--°/--°\n--%", status_code=500)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
