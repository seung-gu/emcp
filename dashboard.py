"""/dashboard 페이지 렌더링. 마크업과 스타일은 dashboard.html 에 있다."""

from __future__ import annotations

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


def rssi_label(rssi: int) -> str:
    for floor, name in RSSI_BANDS:
        if rssi >= floor:
            return name
    return _WEAKEST


def _hhmm(iso: str) -> str:
    t = datetime.fromisoformat(iso)
    return f"{t.month}/{t.day} {t.hour:02d}:{t.minute:02d}"


def _chart(rows: list[dict], key: str, unit: str, lo: int | None = None,
           hi: int | None = None, guides: tuple = (),
           width: int = 720, height: int = 160) -> str:
    """보고 목록을 SVG 꺾은선으로. 축 눈금은 SVG 밖 HTML 로 둔다 (가로로 늘려도 글자가
    찌그러지지 않게).

    lo/hi 를 주면 축을 고정한다. 신호세기처럼 절대값에 의미가 있는 값은 자동 스케일로
    그리면 평평한 구간이 요동처럼 보인다.
    """
    # 이 값이 없는 보고는 뺀다. 필드는 나중에 생기기도 하므로 옛 행에는 없고, 한 행만 비어도
    # 페이지 전체가 못 그려진다. rows 를 먼저 걸러야 아래 x축 라벨이 실제로 그린 점과 맞는다.
    rows = [r for r in rows if isinstance(r.get(key), (int, float))]
    values = [r[key] for r in rows]
    if len(values) < 2:
        return '<p class="empty">그래프를 그리려면 보고가 2건 이상 필요합니다.</p>'
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = hi - lo or 1

    def y(v: float) -> float:
        v = max(lo, min(hi, v))                      # 축 밖 값은 가장자리에 붙인다
        return height - (v - lo) / span * (height - 20) - 10

    step = width / (len(values) - 1)
    pts = " ".join(f"{i * step:.1f},{y(v):.1f}" for i, v in enumerate(values))
    lines = "".join(
        f'<line x1="0" x2="{width}" y1="{y(g):.1f}" y2="{y(g):.1f}" stroke="currentColor"'
        f' stroke-width="1" stroke-dasharray="3 5" opacity=".25"/>'
        for g, _ in guides
    )
    # 눈금 글자는 SVG 밖에 둔다. preserveAspectRatio="none" 이라 안에 넣으면 가로로 늘어난다.
    bands = "".join(
        f'<span class="band" style="top:{y(g) / height * 100:.1f}%">{name} {g}</span>'
        for g, name in guides
    )
    mid = rows[len(rows) // 2]
    return (
        f'<div class="chart">'
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img">'
        f'{lines}<polyline points="{pts}" fill="none" stroke="currentColor" stroke-width="2"/>'
        f"</svg>"
        f'<span class="ymax">{hi} {unit}</span><span class="ymin">{lo} {unit}</span>{bands}'
        f"</div>"
        f'<div class="xaxis"><span>{_hhmm(rows[0]["at"])}</span>'
        f'<span>{_hhmm(mid["at"])}</span><span>{_hhmm(rows[-1]["at"])}</span></div>'
    )


def _cell(v) -> str:
    return "" if v is None else str(v)


def _reset_cell(code) -> str:
    """정상 웨이크(딥슬립)는 비워 둔다. 604줄이 전부 '딥슬립'이면 눈에 띄어야 할 나머지가
    묻힌다 — 이 칸은 뭔가 다른 일이 있었다는 표시로만 쓴다."""
    if code is None or code == _NORMAL_RESET:
        return ""
    return RESET_KO.get(code, f"코드 {code}")


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
    """보고에 실려 온 로그. 기기에 시계가 없어 항목마다 시각이 없으므로, 이것들을 데려온
    보고의 도착 시각 아래에 순서대로 묶는다."""
    blocks = [
        f'<div class="logs"><b>{_hhmm(r["at"])}</b> 보고가 데려온 {len(r["log"])}건'
        f'<ol>{"".join(_log_entry(e) for e in r["log"])}</ol></div>'
        for r in recent if r.get("log")
    ]
    return "".join(blocks) or '<p class="empty">놓친 wake 가 없습니다.</p>'


def _attach_awake(rows: list[dict]) -> None:
    """기기가 보내는 prev_awake_ms 는 그 보고가 아니라 **그 앞** wake 를 잰 값이다. 자기 wake
    는 화면까지 그린 뒤에야 끝나는데 보고는 그전에 떠나서 그렇다. 한 칸 당겨서 실제로 잰 행에
    붙인다.

    wifi_attempts 가 1 일 때만 옮긴다. 1 보다 크면 그 사이에 서버까지 못 간 wake 가 있었다는
    뜻이고, 그러면 값은 앞 행이 아니라 그 실패한 wake 를 잰 것이다. 가장 최근 보고는 아직
    다음 보고가 없으므로 자기 wake 시간을 모른 채로 남는다."""
    for cur, nxt in zip(rows, rows[1:]):
        if nxt.get("wifi_attempts") == 1 and (v := nxt.get("prev_awake_ms")) is not None:
            cur["awake_ms"] = v


def render(rows: list[dict]) -> str:
    _attach_awake(rows)
    recent = rows[-50:][::-1]
    table = "".join(
        f"<tr><td>{r['at'].replace('T', ' ').replace('+00:00', '')}</td>"
        f"<td>{_cell(r.get('battery_mv'))}</td><td>{_cell(r.get('wifi_ms'))}</td>"
        f"<td>{_cell(r.get('rssi'))}</td><td>{rssi_label(r['rssi'])}</td>"
        f"<td>{_cell(r.get('chip_c'))}</td><td>{_cell(r.get('awake_ms'))}</td>"
        f"<td>{_reset_cell(r.get('reset_reason'))}</td>"
        f"<td>{len(r.get('log') or []) or ''}</td></tr>"
        for r in recent
    ) or '<tr><td colspan="9" class="empty">아직 보고가 없습니다.</td></tr>'

    last = rows[-1] if rows else {}
    bits = [f"보고 {len(rows)}건"]
    if last.get("fw"):
        bits.append(f"펌웨어 {last['fw']}")
    if (free := last.get("nvs_free")) is not None:
        total = last.get("nvs_total")
        bits.append(f"NVS {free}/{total} 엔트리 남음" if total else f"NVS {free}엔트리 남음")

    return _TEMPLATE.substitute(
        sub=" · ".join(bits),
        battery=_chart(rows, "battery_mv", "mV"),
        rssi=_chart(rows, "rssi", "dBm", lo=-100, hi=-40, guides=RSSI_BANDS),
        rssi_note=f"0 에 가까울수록 세다. 맨 아래 점선보다 낮으면 {_WEAKEST}.",
        awake=_chart(rows, "awake_ms", "ms"),
        chip=_chart(rows, "chip_c", "°C"),
        table=table,
        logs=_logs(recent),
    )
