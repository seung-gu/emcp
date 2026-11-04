"""/dashboard 페이지를 만든다. 마크업과 스타일은 dashboard.html 에 있다."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TEMPLATE = Template((Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"))

# RSSI 구간. 보드의 rssiLevel() (display.cpp) 과 같은 경계를 쓴다.
RSSI_BANDS = ((-55, "강함"), (-65, "좋음"), (-75, "보통"), (-85, "약함"))
_WEAKEST = "매우 약함"


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

# 보드가 가끔 2 mV 같은 값을 보낸다. 리튬 셀은 그 전에 보호 회로가 끊어서 이 전압까지 내려가지
# 않으니, 측정 실패로 보고 버린다. 저장된 값은 그대로 두고 화면에서만 빼므로, 기준을 바꾸면 지난
# 보고도 다시 판정된다.
_BATTERY_MIN_MV = 2000


def rssi_label(rssi: int) -> str:
    for floor, name in RSSI_BANDS:
        if rssi >= floor:
            return name
    return _WEAKEST


# 시각은 그 보고 때 날씨를 받은 위치의 현지 시각으로 보여 준다. 시간대는 board_api.py 가 날씨와 함께 보고에
# 남긴다. 시간대가 기록되기 전 보고는 그 보드의 가장 최근 시간대를, 그것도 없으면 기본 위치(뮌헨)의 시간대를
# 쓴다. 그때까지의 보고는 모두 뮌헨 날씨였다.
_DEFAULT_TZ = "Europe/Berlin"


@cache
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(_DEFAULT_TZ)


def _attach_tz(rows: list[dict]) -> None:
    """보고마다 보여 줄 시간대를 tz 에 붙인다."""
    latest: dict = {}
    for r in reversed(rows):
        if r.get("timezone"):
            latest.setdefault(r.get("mac"), r["timezone"])
    for r in rows:
        r["tz"] = r.get("timezone") or latest.get(r.get("mac")) or _DEFAULT_TZ


def _local(r: dict) -> datetime:
    return datetime.fromisoformat(r["received_at"]).astimezone(_zone(r["tz"]))


def _hhmm(r: dict) -> str:
    t = _local(r)
    return f"{t.month}/{t.day} {t.hour:02d}:{t.minute:02d}"


# 그래프로 그리는 값. 그래프마다 그 값이 있는 보고만 점으로 찍는다.
_PLOTTED = ("battery_mv", "rssi", "awake_ms", "chip_c", "room_c", "room_rh", "nvs_used")


def _plot_data(rows: list[dict], mac: str | None, nvs_total: int | None) -> str:
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
        "tz": rows[-1]["tz"] if rows else _DEFAULT_TZ,
    }
    if not mac:
        data["mac"] = [r.get("mac") for r in rows]
        data["name"] = [r.get("device_name") for r in rows]
    if nvs_total:
        data["fixed"]["nvs_used"] = [0, nvs_total]
    # 보드 이름은 사람이 정한 글자다. 값 안의 </script> 나 <!-- 가 script 태그를 깨지 않게 < 를 모두 바꾼다.
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _cell(v) -> str:
    return "" if v is None else str(v)


def _board(name: str | None, mac: str | None) -> str:
    """보드 표시. 이름이 있으면 이름을 보이고 마우스를 올리면 바로 MAC 이 뜬다 (dashboard.html 의
    .board). 브라우저 기본 툴팁(title)은 1초쯤 가만히 있어야 떠서 쓰지 않는다. 이름은 사람이 정한 글자라
    이스케이프한다. MAC 은 checks.py 가 이미 걸러서 넣는다."""
    if not name:
        return _cell(mac)
    data_mac = f' data-mac="{mac}"' if mac else ""
    return f'<span class="board"{data_mac}>{html.escape(name)}</span>'


def _reset_cell(code) -> str:
    """딥슬립에서 정상으로 깨어난 경우는 비워 둔다. 거의 모든 행이 '딥슬립'이라 다 찍으면 정작 봐야
    할 행이 묻힌다. 이 칸은 다른 일이 있었을 때만 채운다."""
    if code is None or code == _NORMAL_RESET:
        return ""
    return RESET_KO.get(code, f"코드 {code}")


ROOM_ERR_KO = {"probe": "주소무응답", "write": "명령거부", "read": "읽기실패", "crc": "CRC불일치"}


def _i2c_cell(r: dict) -> str:
    """센서 읽기가 실패한 이유. _reset_cell 과 같은 이유로 정상이면 비워 둔다. 거의 모든 행이
    정상이라 'ok' 를 다 찍으면 드문 실패가 묻힌다.

    진단 값이 아예 없는 보고는 그 값을 보낼 줄 모르는 펌웨어의 보고라서, 정상과 구분해 물음표로
    둔다. 끊김 표시(SDA↓/SCL↓)는 모듈의 풀업이 내부 풀다운에 졌다는 뜻이다. 선이 끊겼거나
    모듈에 전원이 없는 경우다. 성공한 행에 이 표시가 뜨면 풀업이 45k 보다 약한 것이라 표시를
    무시해도 된다."""
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
    """로그 한 줄. 종류마다 실려 오는 값이 달라서 있는 것만 뒤에 붙인다."""
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
    """보내지 못했던 보고들. 보드에 시계가 없어 항목마다 시각이 없으므로, 이것들을 싣고 온 보고의
    도착 시각 아래에 순서대로 묶는다."""
    blocks = [
        f'<div class="logs"><b>{_hhmm(r)}</b> 보고에 실려 온 {len(r["log"])}건'
        f'<ol>{"".join(_log_entry(e) for e in r["log"])}</ol></div>'
        for r in recent if r.get("log")
    ]
    return "".join(blocks) or '<p class="empty">못 보낸 보고가 없습니다.</p>'


def _drop_bad_battery(rows: list[dict]) -> None:
    """측정 실패로 보이는 배터리 값을 지운다. 그러면 값이 없는 보고와 같아져서 그래프에서는 빠지고
    표에서는 빈칸이 된다.

    그래프는 축을 자동으로 맞추기 때문에, 2 mV 한 건이 있으면 축 아래 끝이 거기까지 내려간다.
    그러면 나머지 3000 mV 대 값이 전부 맨 위에 눌려 직선처럼 보인다."""
    for row in rows:
        for holder in (row, *(row.get("log") or ())):
            if (v := holder.get("battery_mv")) is not None and v < _BATTERY_MIN_MV:
                del holder["battery_mv"]


def _attach_awake(rows: list[dict]) -> None:
    """보드가 보내는 prev_awake_ms 는 그 보고 직전 wake 를 잰 값이다. 한 번 깨어난 시간은 화면을
    다 그린 뒤에야 끝나는데, 보고는 그전에 보내기 때문이다. 그래서 한 칸 당겨 실제로 잰 행에 붙인다.

    wifi_attempts 가 1 일 때만 옮긴다. 1 보다 크면 그 사이에 서버에 닿지 못한 wake 가 있었다는
    뜻이고, 그러면 그 값은 앞 행의 wake 가 아닌 실패한 wake 를 잰 것이다. 가장 최근 보고는 다음
    보고가 아직 없어서 자기 wake 시간을 모른 채로 남는다.

    0 은 버린다. NVS 가 비어 있던 첫 부팅이 기본값으로 보낸 값이고, 옛 보고에 남아 있다.

    보드별로 묶은 뒤에 당긴다. 여러 대가 섞여 있으면 바로 다음 행이 다른 보드의 보고라, 그냥
    당기면 남의 wake 시간을 붙이게 된다. mac 이 없는 옛 보고는 한 묶음으로 처리하는데, 그 구간에
    두 대가 섞여 있었다면 그 값은 바로잡을 수 없다."""
    by_mac: dict = {}
    for r in rows:
        by_mac.setdefault(r.get("mac"), []).append(r)
    for group in by_mac.values():
        for cur, nxt in zip(group, group[1:]):
            if nxt.get("wifi_attempts") == 1 and (v := nxt.get("prev_awake_ms")):
                cur["awake_ms"] = v


def _attach_nvs(rows: list[dict]) -> int | None:
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


def _device_links(rows: list[dict], mac: str | None) -> str:
    """보고를 보낸 적 있는 보드 목록. 여러 대가 섞여 있을 때 한 대만 골라 보는 버튼이다."""
    seen = {}
    for r in rows:
        if r.get("mac"):
            seen[r["mac"]] = (r, r.get("device_name"))
    if not seen:
        return ""
    links = [f'<a href="/dashboard"{" class=\'on\'" if not mac else ""}>전체</a>']
    for m, (last, name) in sorted(seen.items()):
        on = " class='on'" if m == mac else ""
        links.append(f'<a href="/dashboard?mac={m}"{on}>{_board(name, m)}<span class="dim">{_hhmm(last)}</span></a>')
    return f'<p class="devices">{"".join(links)}</p>'


def _history_offer(h: dict) -> str:
    """등록된 보드에 같은 이메일의 예전 기록이 있을 때 가져올지 묻는 줄. 값은 모두 DB 가 만든 숫자와 시각이다."""
    span = f'{h["first"]:%Y-%m-%d} ~ {h["last"]:%Y-%m-%d}'
    def button(choice: str, label: str) -> str:
        return (f'<form method="post" action="/dashboard/history"><input type="hidden" name="device_id"'
                f' value="{h["device_id"]}"><input type="hidden" name="choice" value="{choice}">'
                f'<button type="submit">{label}</button></form>')
    return (f'<tr class="offer"><td colspan="4">이 보드의 예전 기록 {h["reports"]:,}건({span})이 있습니다.'
            f' 가져올까요? {button("import", "가져오기")}{button("keep", "그냥 두기")}</td></tr>')


def _rename_form(b: dict) -> str:
    """이름 바꾸기. 펼치면 지금 이름이 들어 있는 칸이 나온다."""
    current = html.escape(b["name"] or "", quote=True)
    return ('<details class="edit"><summary>이름 바꾸기</summary>'
            '<form method="post" action="/dashboard/rename">'
            f'<input type="hidden" name="device_id" value="{b["id"]}">'
            f'<input name="name" maxlength="40" required value="{current}" aria-label="보드 이름">'
            '<button type="submit">저장</button></form></details>')


def _location_form(b: dict) -> str:
    """위치 바꾸기. 도시를 검색하면 dashboard.html 의 스크립트가 후보를 띄우고, 하나를 고르면 저장 칸이 채워진다.
    같은 이름의 다른 도시를 잘못 고르지 않게 지역과 나라를 같이 보여 준다."""
    return ('<details class="edit"><summary>위치 바꾸기</summary>'
            '<form class="place-search"><input name="q" maxlength="60" required'
            ' placeholder="도시 이름 (예: Berlin)" aria-label="도시 검색"><button type="submit">검색</button></form>'
            '<form class="place-pick" method="post" action="/dashboard/location" hidden>'
            f'<input type="hidden" name="device_id" value="{b["id"]}">'
            '<input type="hidden" name="name"><input type="hidden" name="lat"><input type="hidden" name="lon">'
            '<ul class="places"></ul><button type="submit" disabled>저장</button></form></details>')


def _share_form(b: dict, shared: list[dict]) -> str:
    """공유. 공유한 이메일마다 상태(수락 대기 / 공유 중)와 취소 버튼, 그 아래 새 이메일 칸. 이메일은 사람이 넣은
    글자라 이스케이프한다."""
    items = []
    for s in shared:
        email = html.escape(s["email"], quote=True)
        state = "공유 중" if s["accepted"] else "수락 대기"
        items.append(f'<li>{email}<span class="dim">{state}</span>'
                     '<form method="post" action="/dashboard/unshare">'
                     f'<input type="hidden" name="device_id" value="{b["id"]}">'
                     f'<input type="hidden" name="email" value="{email}">'
                     '<button type="submit">취소</button></form></li>')
    listing = f'<ul class="shares">{"".join(items)}</ul>' if items else ""
    return ('<details class="edit"><summary>공유' + (f" {len(shared)}" if shared else "") + '</summary>'
            + listing
            + '<form method="post" action="/dashboard/share">'
            f'<input type="hidden" name="device_id" value="{b["id"]}">'
            '<input name="email" type="email" maxlength="254" required placeholder="친구 이메일"'
            ' aria-label="공유할 이메일"><button type="submit">공유하기</button></form>'
            '<p class="note">가입한 사람의 이메일을 넣으면 그 사람에게 공유 요청이 갑니다. 그 사람이 수락하면'
            ' 이 보드를 볼 수 있습니다. 바꾸기는 주인만 할 수 있습니다.</p>'
            '</details>')


def _incoming(requests: list[dict]) -> str:
    """받은 공유 요청. 수락해야 그 보드가 보인다. 이름과 이메일은 사람이 정한 글자라 이스케이프한다."""
    items = []
    for r in requests:
        board = html.escape(r["name"] or r["mac"] or "이름 없는 보드")
        owner = html.escape(r["owner_email"] or "알 수 없는 사람")
        def button(choice: str, label: str) -> str:
            return (f'<form method="post" action="/dashboard/share-answer"><input type="hidden" name="device_id"'
                    f' value="{r["id"]}"><input type="hidden" name="choice" value="{choice}">'
                    f'<button type="submit">{label}</button></form>')
        items.append(f'<li>{owner} 님이 \'{board}\' 보드를 공유하려고 합니다. '
                     f'{button("accept", "수락")}{button("decline", "거절")}</li>')
    return f'<ul class="incoming">{"".join(items)}</ul>' if items else ""


def _shared_section(shared: list[dict], incoming: list[dict]) -> str:
    """공유받은 보드와 받은 공유 요청. 내 보드와 섞지 않고 따로 보여 준다. 둘 다 없으면 칸을 그리지 않는다.
    이름, 도시, 이메일은 사람이 정한 글자라 이스케이프한다."""
    if not shared and not incoming:
        return ""
    rows = []
    for b in shared:
        where = html.escape(b["city"]) if b["city"] else '<span class="dim">기본값</span>'
        state = "" if b["registered"] else ' <span class="dim">(등록 안 됨)</span>'
        leave = ('<form method="post" action="/dashboard/leave" onsubmit="return confirm('
                 "'이 보드를 목록에서 뺄까요? 다시 보려면 주인이 공유를 다시 요청해야 합니다.')\">"
                 f'<input type="hidden" name="device_id" value="{b["id"]}">'
                 '<button type="submit">목록에서 빼기</button></form>')
        rows.append(f"<tr><td>{_board(b['name'], b['mac']) or 'MAC 없음'}{state}</td><td>{where}</td>"
                    f"<td>{html.escape(b.get('owner_email') or '알 수 없음')}</td><td>{leave}</td></tr>")
    table = ('<table class="boards"><tr><th>보드</th><th>날씨 위치</th><th>주인</th><th></th></tr>'
             + "".join(rows) + "</table>") if rows else ""
    return "<h2>공유받은 보드</h2>" + _incoming(incoming) + table


def _boards_section(boards: list[dict], pending: list[dict], notice: str | None, prefill: str | None,
                    history: list[dict], shares: dict, incoming: list[dict]) -> str:
    """내 보드 목록과 이름·위치 바꾸기, 공유, 등록 해제 버튼, 보드 추가(등록 코드 입력 칸), 입력해 두고 보드를
    기다리는 코드. 예전 기록을 가져올 수 있는 보드에는 그 아래에 묻는 줄을 붙인다. 공유받은 보드는 그 아래 따로
    보여 준다. 보드 이름과 도시는 사람이 정한 글자라 이스케이프한다. 코드는 checks.py 가 정한 글자만 통과한다."""
    offers = {h["device_id"]: h for h in history}
    own = [b for b in boards if b["role"] == "owner"]
    lines = []
    for b in own:
        where = html.escape(b["city"]) if b["city"] else '<span class="dim">기본값</span>'
        if not b["registered"]:
            state, action = '<span class="dim">등록 안 됨</span>', _share_form(b, shares.get(b["id"], []))
        else:
            state = "등록됨"
            action = _share_form(b, shares.get(b["id"], [])) + (
                '<form method="post" action="/dashboard/release" onsubmit="return confirm('
                "'이 보드의 등록을 해제할까요? 보드는 다음 보고 때 새 코드를 띄우고, 지난 기록은 계속 볼 수 있습니다.')\">"
                f'<input type="hidden" name="device_id" value="{b["id"]}">'
                '<button type="submit">등록 해제</button></form>'
            )
        name_cell = (_board(b["name"], b["mac"]) or "MAC 없음") + _rename_form(b)
        where_cell = where + _location_form(b)
        lines.append(f"<tr><td>{name_cell}</td><td>{where_cell}</td><td>{state}</td><td>{action}</td></tr>")
        if b["id"] in offers:
            lines.append(_history_offer(offers[b["id"]]))
    table = (
        '<table class="boards"><tr><th>보드</th><th>날씨 위치</th><th>상태</th><th></th></tr>'
        + "".join(lines) + "</table>"
    ) if lines else '<p class="empty">아직 등록한 보드가 없습니다.</p>'

    now = datetime.now(timezone.utc)
    waiting = "".join(
        f'<li><b>{p["code"]}</b> 보드를 기다리는 중'
        f'<span class="dim">{max(1, int((p["expires_at"] - now).total_seconds() // 60))}분 남음</span></li>'
        for p in pending
    )
    # 보드 추가 칸은 접어 둔다. 보드가 아직 없거나, QR 로 와서 코드가 채워졌거나, 기다리는 코드가 있으면 편다.
    add_open = " open" if (not own or prefill or waiting) else ""
    return (
        (f'<p class="notice">{notice}</p>' if notice else "")
        + "<h2>내 보드</h2>" + table
        + f'<details class="add"{add_open}><summary>보드 추가</summary>'
        + '<form class="claim" method="post" action="/dashboard/claim">'
          '<input name="code" maxlength="10" autocomplete="off" autocapitalize="characters" spellcheck="false"'
          f' placeholder="4J7K2P" aria-label="등록 코드" value="{prefill or ""}">'
          '<button type="submit">보드 등록</button></form>'
        + '<p class="note">보드 디스플레이의 QR을 찍거나 6자리 코드를 넣은 뒤, 보드의 RESET 버튼을 한 번'
          ' 누르세요.</p>'
        + (f'<ul class="waiting">{waiting}</ul>' if waiting else "")
        + "</details>"
        + _shared_section([b for b in boards if b["role"] != "owner"], incoming)
    )


def render(rows: list[dict], mac: str | None = None, boards: list[dict] = (), pending: list[dict] = (),
           notice: str | None = None, prefill: str | None = None, history: list[dict] = (),
           shares: dict | None = None, incoming: list[dict] = ()) -> str:
    # 보드별로 나눠야 하는 처리(_attach_awake)는 전체를 대상으로 한 뒤에 거른다.
    _drop_bad_battery(rows)
    _attach_awake(rows)
    _attach_tz(rows)
    devices = _device_links(rows, mac)
    if mac:
        rows = [r for r in rows if r.get("mac") == mac]
    nvs_total = _attach_nvs(rows)
    recent = rows[-50:][::-1]
    # 보드 열은 여러 대를 섞어 볼 때만 둔다. 한 대만 볼 때는 모든 행이 같은 값이라 자리만 차지한다.
    mac_cell = (lambda r: "") if mac else (lambda r: f"<td>{_board(r.get('device_name'), r.get('mac'))}</td>")
    table = "".join(
        f"<tr><td>{_local(r):%Y-%m-%d %H:%M}<span class=\"dim\">{_local(r):%Z}</span></td>"
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
    bits = [f"보고 {len(rows)}건" + ("" if mac else " (전체 보드)")]
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

    return _TEMPLATE.substitute(
        boards=_boards_section(list(boards), list(pending), notice, prefill, list(history), shares or {},
                               list(incoming)),
        devices=devices,
        mac_th="" if mac else "<th>보드</th>",
        sub=" · ".join(bits),
        data=_plot_data(rows, mac, nvs_total),
        rssi_note=f"0에 가까울수록 신호가 셉니다. 맨 아래 점선보다 낮으면 '{_WEAKEST}'입니다.",
        tz_note=f"시각은 날씨 위치의 현지 시각({rows[-1]['tz'] if rows else _DEFAULT_TZ})입니다.",
        table=table,
        logs=_logs(recent),
    )
