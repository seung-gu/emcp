"""/dashboard 페이지 렌더링. 마크업과 스타일은 dashboard.html 에 있다."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from string import Template

_TEMPLATE = Template((Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"))

# RSSI 구간. 기기의 rssiLevel() (display.cpp) 과 같은 경계를 쓴다.
RSSI_BANDS = ((-55, "강함"), (-65, "좋음"), (-75, "보통"), (-85, "약함"))
_WEAKEST = "매우 약함"


# esp_reset_reason() -> 사람이 읽을 이름. 8 이 타이머 웨이크, 곧 정상이다. 1 은 전원이
# 끊겼다 들어왔다는 뜻이고 9 는 배터리가 처지고 있다는 신호, 4~7 은 펌웨어가 죽은 것이다.
RESET_KO = {
    0: "알수없음", 1: "전원투입", 2: "외부핀", 3: "SW재시작", 4: "패닉",
    5: "INT WDT", 6: "TASK WDT", 7: "WDT", 8: "딥슬립", 9: "브라운아웃", 10: "SDIO",
}
_NORMAL_RESET = 8

LOG_KIND_KO = {
    "wifi": "Wi-Fi 실패", "http": "서버 응답 실패", "portal-new": "최초 설정 포털",
    "portal-lost": "네트워크 상실, 설정 포털", "portal-timeout": "포털 시간초과 → 무기한 대기",
    "full": "로그 가득 참 (이후 기록 없음)", "unknown": "알수없음",
}

# wl_status_t. 이 경로에서 실제로 나올 수 있는 값만 적는다.
WIFI_STATUS_KO = {0: "시작 못함", 1: "SSID 없음", 4: "인증 실패", 6: "연결 끊김"}

# 기기가 가끔 2 mV 같은 값을 올린다. 리튬셀은 보호회로가 그전에 끊으므로 이 전압까지
# 내려가지 않는다. 전압이 아니라 측정 실패로 보고 버린다. 파일에는 그대로 두고 화면에서만
# 빼므로 기준을 바꾸면 지난 보고도 다시 판정된다.
_BATTERY_MIN_MV = 2000


def rssi_label(rssi: int) -> str:
    for floor, name in RSSI_BANDS:
        if rssi >= floor:
            return name
    return _WEAKEST


def _hhmm(iso: str) -> str:
    t = datetime.fromisoformat(iso)
    return f"{t.month}/{t.day} {t.hour:02d}:{t.minute:02d}"


# 그래프로 그리는 값. 그래프마다 자기 값이 있는 보고만 점으로 찍힌다.
_PLOTTED = ("battery_mv", "rssi", "awake_ms", "chip_c", "room_c", "room_rh", "nvs_used")


def _plot_data(rows: list[dict], mac: str | None, nvs_total: int | None) -> str:
    """그래프가 쓸 값을 열 단위 JSON 으로. 그리는 건 브라우저가 한다 — 구간을 끌어 고르고
    확대·축소하려면 화면에서 다시 그려야 해서다.

    시각은 epoch 초라 x축이 보고 순번이 아니라 실제 시각이다. 보고가 끊긴 구간은 빈 채로 보인다.
    """
    data = {
        "t": [int(datetime.fromisoformat(r["at"]).timestamp()) for r in rows],
        "fw": [r.get("fw") for r in rows],
        "series": {k: [r.get(k) for r in rows] for k in _PLOTTED},
        # 신호세기처럼 절대값에 의미가 있는 값은 축을 고정한다. 자동으로 맞추면 평평한 구간이
        # 요동처럼 보인다.
        "fixed": {"rssi": [-100, -40]},
        "guides": {"rssi": [[g, name] for g, name in RSSI_BANDS]},
    }
    if not mac:
        data["mac"] = [r.get("mac") for r in rows]
    if nvs_total:
        data["fixed"]["nvs_used"] = [0, nvs_total]
    # 값 안에 </script> 가 있으면 태그가 거기서 닫힌다.
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _cell(v) -> str:
    return "" if v is None else str(v)


def _reset_cell(code) -> str:
    """정상 웨이크(딥슬립)는 비워 둔다. 604줄이 전부 '딥슬립'이면 눈에 띄어야 할 나머지가
    묻힌다 — 이 칸은 뭔가 다른 일이 있었다는 표시로만 쓴다."""
    if code is None or code == _NORMAL_RESET:
        return ""
    return RESET_KO.get(code, f"코드 {code}")


ROOM_ERR_KO = {"probe": "주소무응답", "write": "명령거부", "read": "읽기실패", "crc": "CRC불일치"}


def _i2c_cell(r: dict) -> str:
    """센서 읽기가 실패한 이유. _reset_cell 과 같은 이유로 정상이면 빈 칸이다 — 거의 모든
    행이 정상이라 'ok' 를 전부 찍으면 드문 쪽이 묻힌다.

    진단 필드가 아예 없는 보고는 그걸 보낼 줄 모르는 펌웨어라, 정상과 구분해 물음표로 둔다.
    끊김 표시(SDA↓/SCL↓)는 모듈 풀업이 내부 풀다운에 졌다는 뜻 — 선이 끊겼거나 모듈에
    전원이 없다. 성공한 행에 이게 뜨면 풀업이 45k 보다 약한 거라 그 표시 자체가 무의미하다."""
    if r.get("room_err") is None and r.get("room_stuck") is None:
        return '<span class="dim">?</span>'
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


def _log_entry(e: dict) -> str:
    """로그 한 줄. 종류마다 실려 오는 필드가 다르므로 있는 것만 뒤에 붙인다."""
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
    detail = f'<span class="dim">{" · ".join(bits)}</span>' if bits else ""
    return f'<li>{LOG_KIND_KO.get(e["kind"], "알수없음")}{detail}</li>'


def _logs(recent: list[dict]) -> str:
    """보내지 못했던 보고들. 기기에 시계가 없어 항목마다 시각이 없으므로, 이것들을 데려온
    보고의 도착 시각 아래에 순서대로 묶는다."""
    blocks = [
        f'<div class="logs"><b>{_hhmm(r["at"])}</b> 보고가 뒤늦게 데려온 {len(r["log"])}건'
        f'<ol>{"".join(_log_entry(e) for e in r["log"])}</ol></div>'
        for r in recent if r.get("log")
    ]
    return "".join(blocks) or '<p class="empty">못 보낸 보고가 없습니다.</p>'


def _drop_bad_battery(rows: list[dict]) -> None:
    """측정 실패로 보이는 배터리 값을 지운다. 값이 아예 없는 보고와 같아져서 그래프에서는
    빠지고 표에서는 빈 칸이 된다.

    그래프는 자동 스케일이라 2 mV 한 건이 축 아래끝을 거기까지 내린다. 그러면 나머지
    3000 mV 대가 전부 맨 위에 눌린 직선이 된다."""
    for row in rows:
        for holder in (row, *(row.get("log") or ())):
            if (v := holder.get("battery_mv")) is not None and v < _BATTERY_MIN_MV:
                del holder["battery_mv"]


def _attach_awake(rows: list[dict]) -> None:
    """기기가 보내는 prev_awake_ms 는 그 보고가 아니라 **그 앞** wake 를 잰 값이다. 자기 wake
    는 화면까지 그린 뒤에야 끝나는데 보고는 그전에 떠나서 그렇다. 한 칸 당겨서 실제로 잰 행에
    붙인다.

    wifi_attempts 가 1 일 때만 옮긴다. 1 보다 크면 그 사이에 서버까지 못 간 wake 가 있었다는
    뜻이고, 그러면 값은 앞 행이 아니라 그 실패한 wake 를 잰 것이다. 가장 최근 보고는 아직
    다음 보고가 없으므로 자기 wake 시간을 모른 채로 남는다.

    0 은 버린다. NVS 가 비어 있던 첫 부팅이 기본값으로 보낸 값이고, 옛 보고에 남아 있다.

    기기별로 묶고 나서 당긴다. 여러 대가 한 파일에 섞이면 바로 다음 행은 다른 기기의 보고라,
    그냥 당기면 남의 wake 시간을 가져다 붙인다. mac 이 없는 옛 보고는 한 덩어리로 묶이는데,
    그 구간에 두 대가 섞여 있었다면 그 값들은 바로잡을 방법이 없다."""
    by_mac: dict = {}
    for r in rows:
        by_mac.setdefault(r.get("mac"), []).append(r)
    for group in by_mac.values():
        for cur, nxt in zip(group, group[1:]):
            if nxt.get("wifi_attempts") == 1 and (v := nxt.get("prev_awake_ms")):
                cur["awake_ms"] = v


def _attach_nvs(rows: list[dict]) -> int | None:
    """남은 칸을 쓴 칸으로 뒤집는다. 차오르는 그림이 "채워지고 있나"를 묻기에 자연스럽고,
    축을 0~전체로 고정하면 여유가 얼마나 남았는지가 선의 높이로 바로 보인다.

    전체 칸수는 기기마다 고정이라, 한 번이라도 실려 온 값을 나머지 행에도 쓴다 — 이 필드가
    생기기 전의 보고에는 남은 칸만 있다."""
    total = next((r["nvs_total"] for r in reversed(rows) if r.get("nvs_total")), None)
    if total:
        for r in rows:
            if (free := r.get("nvs_free")) is not None:
                r["nvs_used"] = total - free
    return total


def _device_links(rows: list[dict], mac: str | None) -> str:
    """보고를 보낸 적 있는 기기 목록. 섞여 있을 때 한 대만 떼어 보는 입구다."""
    seen = {}
    for r in rows:
        if r.get("mac"):
            seen[r["mac"]] = r["at"]
    if not seen:
        return ""
    links = [f'<a href="/dashboard"{" class=\'on\'" if not mac else ""}>전체</a>']
    for m, at in sorted(seen.items()):
        on = " class='on'" if m == mac else ""
        links.append(f'<a href="/dashboard?mac={m}"{on}>{m}<span class="dim">{_hhmm(at)}</span></a>')
    return f'<p class="devices">{"".join(links)}</p>'


def render(rows: list[dict], mac: str | None = None) -> str:
    # 기기별로 갈라야 하는 것(_attach_awake)은 전체를 보고 처리한 뒤에 거른다.
    _drop_bad_battery(rows)
    _attach_awake(rows)
    devices = _device_links(rows, mac)
    if mac:
        rows = [r for r in rows if r.get("mac") == mac]
    nvs_total = _attach_nvs(rows)
    recent = rows[-50:][::-1]
    # mac 열은 섞어서 볼 때만. 한 대만 보는 중이면 모든 행이 같은 값이라 자리만 차지한다.
    mac_cell = (lambda r: "") if mac else (lambda r: f"<td>{_cell(r.get('mac'))}</td>")
    table = "".join(
        f"<tr><td>{r['at'].replace('T', ' ').replace('+00:00', '')}</td>"
        f"{mac_cell(r)}"
        f"<td>{_cell(r.get('battery_mv'))}</td><td>{_cell(r.get('wifi_ms'))}</td>"
        f"<td>{_cell(r.get('rssi'))}</td><td>{rssi_label(r['rssi'])}</td>"
        f"<td>{_cell(r.get('chip_c'))}</td>"
        f"<td>{_cell(r.get('room_c'))}</td><td>{_cell(r.get('room_rh'))}</td>"
        f"<td>{_i2c_cell(r)}</td>"
        f"<td>{_cell(r.get('awake_ms'))}</td>"
        f"<td>{_reset_cell(r.get('reset_reason'))}</td>"
        f"<td>{len(r.get('log') or []) or ''}</td></tr>"
        for r in recent
    ) or f'<tr><td colspan="{12 if mac else 13}" class="empty">아직 보고가 없습니다.</td></tr>'

    last = rows[-1] if rows else {}
    bits = [f"보고 {len(rows)}건" + ("" if mac else " (전체 기기)")]
    if last.get("fw"):
        bits.append(f"펌웨어 {last['fw']}")
    if (free := last.get("nvs_free")) is not None:
        total = last.get("nvs_total")
        bits.append(f"NVS {free}/{total} 엔트리 남음" if total else f"NVS {free}엔트리 남음")
    # 진단을 보내는 펌웨어의 보고만 모아 실패 이유를 센다. 분모가 전체가 아닌 건 그 전
    # 보고들에는 필드가 아예 없어서다 — 거기서는 실패를 이유별로 가를 수 없다.
    knows = [r for r in rows if r.get("room_err")]
    if knows:
        tally = Counter(r["room_err"] for r in knows if r["room_err"] != "ok")
        bits.append("센서 " + (
            " ".join(f"{ROOM_ERR_KO.get(k, k)} {n}" for k, n in tally.most_common())
            + f" / {len(knows)}건" if tally else f"{len(knows)}건 전부 정상"))

    return _TEMPLATE.substitute(
        devices=devices,
        mac_th="" if mac else "<th>기기</th>",
        sub=" · ".join(bits),
        data=_plot_data(rows, mac, nvs_total),
        rssi_note=f"0 에 가까울수록 세다. 맨 아래 점선보다 낮으면 {_WEAKEST}.",
        table=table,
        logs=_logs(recent),
    )
