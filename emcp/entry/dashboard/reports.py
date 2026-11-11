"""대시보드가 보고를 보여 주기 전에 하는 계산. HTML 은 만들지 않고, 템플릿이 그릴 값만 만든다."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# RSSI 구간. 보드의 rssiLevel() (display.cpp) 과 같은 경계를 쓴다.
RSSI_BANDS = ((-55, "강함"), (-65, "좋음"), (-75, "보통"), (-85, "약함"))
WEAKEST = "매우 약함"


# esp_reset_reason() 값을 사람이 읽는 이름으로. 8 은 타이머로 깨어난 것이라 정상이다. 1 은 전원이
# 끊겼다 다시 들어왔다는 뜻, 9 는 배터리가 약해지고 있다는 신호, 4~7 은 펌웨어가 죽은 것이다.
RESET_KO = {
    0: "알수없음", 1: "전원투입", 2: "외부핀", 3: "SW재시작", 4: "패닉",
    5: "INT WDT", 6: "TASK WDT", 7: "WDT", 8: "딥슬립", 9: "브라운아웃", 10: "SDIO",
}
_NORMAL_RESET = 8

LOG_KIND_KO = {
    "wifi": "Wi-Fi 연결 실패", "http": "서버 응답 실패", "portal-new": "처음 설정 페이지 열림",
    "portal-lost": "Wi-Fi를 못 찾아 설정 페이지 열림",
    "portal-timeout": "설정 페이지 시간 초과, RESET을 누를 때까지 잠듦",
    "full": "로그가 가득 참 (이후 기록 없음)", "unknown": "알 수 없음",
}

# wl_status_t. 이 경로에서 실제로 나올 수 있는 값만 적는다.
WIFI_STATUS_KO = {0: "시작 못함", 1: "SSID 없음", 4: "인증 실패", 6: "연결 끊김"}

ROOM_ERR_KO = {"probe": "주소무응답", "write": "명령거부", "read": "읽기실패", "crc": "CRC불일치"}

# 보드가 가끔 2 mV 같은 값을 보낸다. 리튬 셀은 그 전에 보호 회로가 끊어서 이 전압까지 내려가지
# 않으니, 측정 실패로 보고 버린다. 저장된 값은 그대로 두고 화면에서만 빼므로, 기준을 바꾸면 지난
# 보고도 다시 판정된다.
_BATTERY_MIN_MV = 2000


def rssi_label(rssi: int) -> str:
    for floor, name in RSSI_BANDS:
        if rssi >= floor:
            return name
    return WEAKEST


# 시각은 그 보고 때 날씨를 받은 위치의 현지 시각으로 보여 준다. 시간대는 board_api.py 가 날씨와 함께 보고에
# 남긴다. 시간대가 기록되기 전 보고는 그 보드의 가장 최근 시간대를, 그것도 없으면 기본 위치(뮌헨)의 시간대를
# 쓴다. 그때까지의 보고는 모두 뮌헨 날씨였다.
DEFAULT_TZ = "Europe/Berlin"


@cache
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def attach_tz(rows: list[dict]) -> None:
    """보고마다 보여 줄 시간대를 tz 에 붙인다."""
    latest: dict = {}
    for r in reversed(rows):
        if r.get("timezone"):
            latest.setdefault(r.get("device_id"), r["timezone"])
    for r in rows:
        r["tz"] = r.get("timezone") or latest.get(r.get("device_id")) or DEFAULT_TZ


def _local(r: dict) -> datetime:
    return datetime.fromisoformat(r["received_at"]).astimezone(_zone(r["tz"]))


def _hhmm(r: dict) -> str:
    t = _local(r)
    return f"{t.month}/{t.day} {t.hour:02d}:{t.minute:02d}"


# 그래프로 그리는 값. 그래프마다 그 값이 있는 보고만 점으로 찍는다.
_PLOTTED = ("battery_mv", "rssi", "awake_ms", "chip_c", "room_c", "room_rh", "nvs_used")


def plot_data(rows: list[dict], board_id: int | None, nvs_total: int | None) -> str:
    """그래프에 쓸 값을 열 단위 JSON 으로 만든다. 그래프는 브라우저가 그린다. 구간을 끌어 고르고
    확대·축소하려면 화면에서 다시 그려야 하기 때문이다.

    시각은 epoch 초라서 x축이 실제 시각을 따른다. 보고가 끊긴 구간은 비어 보인다.
    """
    data = {
        "t": [int(datetime.fromisoformat(r["received_at"]).timestamp()) for r in rows],
        "fw_version": [r.get("fw_version") for r in rows],
        "series": {k: [r.get(k) for r in rows] for k in _PLOTTED},
        # 신호 세기처럼 값 자체에 뜻이 있는 그래프는 축을 고정한다. 자동으로 맞추면 평평한 구간도
        # 크게 출렁이는 것처럼 보인다.
        "fixed": {"rssi": [-100, -40]},
        "guides": {"rssi": [[g, name] for g, name in RSSI_BANDS]},
        # 그래프 축의 시간대. 가장 최근 보고의 위치를 따른다. 표는 줄마다 자기 시간대로 보여 준다.
        "tz": rows[-1]["tz"] if rows else DEFAULT_TZ,
    }
    if not board_id:
        data["mac"] = [r.get("mac") for r in rows]
        data["name"] = [r.get("device_name") for r in rows]
    if nvs_total:
        data["fixed"]["nvs_used"] = [0, nvs_total]
    # 보드 이름은 사람이 정한 글자다. 값 안의 </script> 나 <!-- 가 script 태그를 깨지 않게 < 를 모두 바꾼다.
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _reset_label(code) -> str:
    """딥슬립에서 정상으로 깨어난 경우는 비워 둔다. 거의 모든 행이 '딥슬립'이라 다 찍으면 정작 봐야
    할 행이 묻힌다. 이 칸은 다른 일이 있었을 때만 채운다."""
    if code is None or code == _NORMAL_RESET:
        return ""
    return RESET_KO.get(code, f"코드 {code}")


def _i2c_label(r: dict) -> str | None:
    """센서 읽기가 실패한 이유. _reset_label 과 같은 이유로 정상이면 비워 둔다. 거의 모든 행이
    정상이라 'ok' 를 다 찍으면 드문 실패가 묻힌다.

    진단 값이 아예 없는 보고는 그 값을 보낼 줄 모르는 펌웨어의 보고라서, 정상과 구분해 None 을 돌려준다.
    화면에는 물음표로 나온다. 끊김 표시(SDA↓/SCL↓)는 모듈의 풀업이 내부 풀다운에 졌다는 뜻이다. 선이
    끊겼거나 모듈에 전원이 없는 경우다. 성공한 행에 이 표시가 뜨면 풀업이 45k 보다 약한 것이라 표시를
    무시해도 된다."""
    if r.get("room_err") is None and r.get("room_stuck") is None:
        return None
    bits = []
    if r.get("room_stuck"):
        bits.append("막힘")
    if r.get("room_sda") is False:
        bits.append("SDA↓")
    if r.get("room_scl") is False:
        bits.append("SCL↓")
    if (err := r.get("room_err")) and err != "ok":
        bits.append(ROOM_ERR_KO.get(err, err))
    if (ack := r.get("room_ack")):
        bits.append(f"ACK {ack}")
    return " ".join(bits)


def _log_detail(e: dict) -> str:
    """로그 한 줄의 뒷부분. 종류마다 실려 오는 값이 달라서 있는 것만 붙인다."""
    bits = []
    if (c := e.get("http_code")) is not None:
        bits.append(f"코드 {c}")
    if (s := e.get("wifi_status")) is not None:
        bits.append(WIFI_STATUS_KO.get(s, f"상태 {s}"))
    for key, unit in (("wifi_ms", " ms"), ("battery_mv", " mV"), ("rssi", " dBm")):
        if (v := e.get(key)) is not None:
            bits.append(f"{v}{unit}")
    if (n := e.get("reset_reason")) is not None and n != _NORMAL_RESET:
        bits.append(RESET_KO.get(n, f"코드 {n}"))
    return " · ".join(bits)


def drop_bad_battery(rows: list[dict]) -> None:
    """측정 실패로 보이는 배터리 값을 지운다. 그러면 값이 없는 보고와 같아져서 그래프에서는 빠지고
    표에서는 빈칸이 된다.

    그래프는 축을 자동으로 맞추기 때문에, 2 mV 한 건이 있으면 축 아래 끝이 거기까지 내려간다.
    그러면 나머지 3000 mV 대 값이 전부 맨 위에 눌려 직선처럼 보인다."""
    for row in rows:
        for holder in (row, *(row.get("log") or ())):
            if (v := holder.get("battery_mv")) is not None and v < _BATTERY_MIN_MV:
                del holder["battery_mv"]


def attach_awake(rows: list[dict]) -> None:
    """보드가 보내는 prev_awake_ms 는 그 보고 직전 wake 를 잰 값이다. 한 번 깨어난 시간은 화면을
    다 그린 뒤에야 끝나는데, 보고는 그전에 보내기 때문이다. 그래서 한 칸 당겨 실제로 잰 행에 붙인다.

    wifi_attempts 가 1 일 때만 옮긴다. 1 보다 크면 그 사이에 서버에 닿지 못한 wake 가 있었다는
    뜻이고, 그러면 그 값은 앞 행의 wake 가 아닌 실패한 wake 를 잰 것이다. 가장 최근 보고는 다음
    보고가 아직 없어서 자기 wake 시간을 모른 채로 남는다.

    0 은 버린다. NVS 가 비어 있던 첫 부팅이 기본값으로 보낸 값이고, 옛 보고에 남아 있다.

    보드별로 묶은 뒤에 당긴다. 여러 대가 섞여 있으면 바로 다음 행이 다른 보드의 보고라, 그냥
    당기면 남의 wake 시간을 붙이게 된다. MAC 을 보내지 않던 옛 보고는 보드 번호 하나에 모여 있어 한 묶음으로
    처리하는데, 그 구간에 두 대가 섞여 있었다면 그 값은 바로잡을 수 없다.

    prev_radio_ms(지난 wake 에서 라디오가 켜져 있던 시간)도 같은 이유로 같은 규칙으로 당겨 radio_ms 로 붙인다."""
    by_board: dict = {}
    for r in rows:
        by_board.setdefault(r.get("device_id"), []).append(r)
    for group in by_board.values():
        for cur, nxt in zip(group, group[1:]):
            if nxt.get("wifi_attempts") != 1:
                continue
            if (v := nxt.get("prev_awake_ms")):
                cur["awake_ms"] = v
            if (v := nxt.get("prev_radio_ms")):
                cur["radio_ms"] = v


def attach_nvs(rows: list[dict]) -> int | None:
    """남은 칸 수를 쓴 칸 수로 바꾼다. 차오르는 그래프가 "채워지고 있나"를 보기에 자연스럽고,
    축을 0부터 전체 칸 수까지 고정하면 여유가 얼마나 남았는지 선의 높이로 바로 보인다.

    전체 칸 수는 보드마다 고정이라, 한 번이라도 실려 온 값을 나머지 행에도 쓴다. 이 값을 보내기
    전의 보고에는 남은 칸 수만 있다."""
    total = next((r["nvs_total"] for r in reversed(rows) if r.get("nvs_total")), None)
    if total:
        for r in rows:
            if (free := r.get("nvs_free")) is not None:
                r["nvs_used"] = total - free
    return total


def device_list(rows: list[dict]) -> list[dict]:
    """보고를 보낸 적 있는 보드 목록, 보드 번호 순. 여러 대가 섞여 있을 때 한 대만 골라 보는 버튼이 된다."""
    seen = {}
    for r in rows:
        seen[r.get("device_id")] = r
    return [{"id": i, "name": last.get("device_name"), "mac": last.get("mac"), "last": _hhmm(last)}
            for i, last in sorted(seen.items(), key=lambda kv: kv[0] or 0)]


def table_rows(recent: list[dict]) -> list[dict]:
    """최근 보고 표의 줄마다 보여 줄 값. 없는 값은 None 이고 빈칸이 된다."""
    out = []
    for r in recent:
        t = _local(r)
        out.append({
            "time": f"{t:%Y-%m-%d %H:%M}", "zone": f"{t:%Z}",
            "name": r.get("device_name"), "mac": r.get("mac"),
            "battery_mv": r.get("battery_mv"), "wifi_ms": r.get("wifi_ms"),
            "rssi": r.get("rssi"), "strength": rssi_label(r["rssi"]),
            "chip_c": r.get("chip_c"), "room_c": r.get("room_c"), "room_rh": r.get("room_rh"),
            "i2c": _i2c_label(r), "awake_ms": r.get("awake_ms"), "radio_ms": r.get("radio_ms"),
            "reset": _reset_label(r.get("reset_reason")),
            "logged": len(r.get("log") or []) or "",
        })
    return out


def log_blocks(recent: list[dict]) -> list[dict]:
    """보내지 못했던 보고들. 보드에 시계가 없어 항목마다 시각이 없으므로, 이것들을 싣고 온 보고의
    도착 시각 아래에 순서대로 묶는다."""
    return [{"time": _hhmm(r), "entries": [{"kind": LOG_KIND_KO.get(e["kind"], "알수없음"),
                                            "detail": _log_detail(e)} for e in r["log"]]}
            for r in recent if r.get("log")]


def summary(rows: list[dict], board_id: int | None) -> str:
    """제목 아래 한 줄: 보고 수, 펌웨어, NVS 여유, 센서 실패 이유."""
    last = rows[-1] if rows else {}
    bits = [f"보고 {len(rows)}건" + ("" if board_id else " (전체 보드)")]
    if last.get("fw_version"):
        bits.append(f"펌웨어 {last['fw_version']}")
    if (free := last.get("nvs_free")) is not None:
        total = last.get("nvs_total")
        bits.append(f"NVS {free}/{total} 엔트리 남음" if total else f"NVS {free}엔트리 남음")
    # 진단 값을 보내는 펌웨어의 보고만 모아 실패 이유를 센다. 전체 보고 수로 나누지 않는 건 그 전
    # 보고에는 진단 값이 아예 없어서 이유별로 나눌 수 없기 때문이다.
    knows = [r for r in rows if r.get("room_err")]
    if knows:
        tally = Counter(r["room_err"] for r in knows if r["room_err"] != "ok")
        bits.append("센서 " + (
            " ".join(f"{ROOM_ERR_KO.get(k, k)} {n}" for k, n in tally.most_common())
            + f" / {len(knows)}건" if tally else f"{len(knows)}건 전부 정상"))
    return " · ".join(bits)
