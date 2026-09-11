"""/dashboard 페이지 렌더링. 마크업과 스타일은 dashboard.html 에 있다."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from string import Template

_TEMPLATE = Template((Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"))

# RSSI 구간. 기기의 rssiLevel() (display.cpp) 과 같은 경계를 쓴다.
RSSI_BANDS = ((-55, "강함"), (-65, "좋음"), (-75, "보통"), (-85, "약함"))
_WEAKEST = "매우 약함"


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


def render(rows: list[dict], keep_days: int, max_reports: int) -> str:
    recent = rows[-50:][::-1]
    table = "".join(
        f"<tr><td>{r['at'].replace('T', ' ').replace('+00:00', '')}</td>"
        f"<td>{r['battery_mv']}</td><td>{r['wifi_ms']}</td>"
        f"<td>{r['rssi']}</td><td>{rssi_label(r['rssi'])}</td></tr>"
        for r in recent
    ) or '<tr><td colspan="5" class="empty">아직 보고가 없습니다.</td></tr>'

    return _TEMPLATE.substitute(
        sub=f"보고 {len(rows)}건 · {keep_days}일 보관 (최대 {max_reports}건)"
            " · 재배포하면 초기화됩니다",
        battery=_chart(rows, "battery_mv", "mV"),
        rssi=_chart(rows, "rssi", "dBm", lo=-100, hi=-40, guides=RSSI_BANDS),
        rssi_note=f"0 에 가까울수록 세다. 맨 아래 점선보다 낮으면 {_WEAKEST}.",
        table=table,
    )
