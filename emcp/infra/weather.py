"""open-meteo 로 날씨와 지명 좌표를 가져온다. API 키가 필요 없다."""

from datetime import datetime, timedelta, timezone

import httpx

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


async def board_body(loc: dict) -> tuple[dict, int, dict]:
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
                  "pop": daily["precipitation_probability_max"][0],
                  "timezone": data.get("timezone")}   # timezone=auto 로 open-meteo 가 알려 준 현지 시간대
        return ({"city": loc["name"], "temp_c": temp, "cond": desc, "wind_kmh": wind,
                 "humidity": humid, "temp_max_c": tmax, "temp_min_c": tmin, "pop": pop,
                 "stamp": stamp,
                 # 4단계 펌웨어가 읽는 새 이름. 옛 펌웨어 보드가 남아 있는 동안 옛 이름과 같이 보낸다
                 "out_temp_c": temp, "out_humidity": humid, "out_temp_max_c": tmax,
                 "out_temp_min_c": tmin}, 200, record)
    except Exception:
        # stamp 도 뺀다: 현지 시각 오프셋이 실패한 그 응답 안에 들어 있어서 서버도 모른다.
        return ({"city": loc["name"]}, 500, {})


async def geocode(query: str, count: int) -> list[dict] | None:
    """open-meteo 로 지명의 좌표 후보를 찾는다. Claude 의 search_location 과 대시보드의 위치 바꾸기가 같이
    쓴다. 조회에 실패하면 None."""
    params = {"name": query, "count": max(1, min(count, 10)), "language": "ko", "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://geocoding-api.open-meteo.com/v1/search", params=params)
            return resp.json().get("results", [])
    except Exception:
        return None
