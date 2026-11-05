"""/dashboard 페이지를 그린다. 값은 reports.py 가 만들고, 마크업은 templates/, 스타일과 스크립트는 static/ 에
있다. 템플릿은 넣는 값을 모두 이스케이프한다(autoescape). 이스케이프하지 않는 건 서버가 만든 그래프 JSON 과
static/ 의 CSS, JS 뿐이다."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from . import reports

_HERE = Path(__file__).parent
_env = Environment(loader=FileSystemLoader(_HERE / "templates"), autoescape=True, undefined=StrictUndefined,
                   trim_blocks=True, lstrip_blocks=True)
# 빈 값은 빈칸으로. 템플릿에 None 을 넣으면 'None' 이 찍힌다.
_env.filters["cell"] = lambda v: "" if v is None else v
_STATIC = {name: Markup((_HERE / "static" / name).read_text(encoding="utf-8"))
           for name in ("page.css", "plots.js", "notice.js", "places.js", "theme.js")}


def render(rows: list[dict], board_id: int | None = None, boards: list[dict] = (), pending: list[dict] = (),
           notice: str | None = None, prefill: str | None = None, history: list[dict] = (),
           shares: dict | None = None, incoming: list[dict] = (), latest: str | None = None,
           up_to_date: dict | None = None) -> str:
    # 보드별로 나눠야 하는 처리(attach_awake)는 전체를 대상으로 한 뒤에 거른다.
    reports.drop_bad_battery(rows)
    reports.attach_awake(rows)
    reports.attach_tz(rows)
    device_list = reports.device_list(rows)
    if board_id:
        rows = [r for r in rows if r.get("device_id") == board_id]
    nvs_total = reports.attach_nvs(rows)
    recent = rows[-50:][::-1]

    own = [b for b in boards if b["role"] == "owner"]
    shares = shares or {}
    # 받은 요청마다, 수락할 때 요청한 사람에게도 보여 줄 수 있는 내 보드: 아직 그 사람에게 공유하지 않은 것
    share_back = {r["id"]: [b for b in own if r["owner_email"] not in {s["email"] for s in shares.get(b["id"], [])}]
                  for r in incoming}
    now = datetime.now(timezone.utc)
    waiting = [{"code": p["code"], "minutes": max(1, int((p["expires_at"] - now).total_seconds() // 60))}
               for p in pending]
    return _env.get_template("page.html").render(
        static=_STATIC,
        board_id=board_id,
        summary=reports.summary(rows, board_id),
        devices=device_list,
        notice=notice,
        own=own,
        shared=[b for b in boards if b["role"] != "owner"],
        incoming=list(incoming),
        offers={h["device_id"]: h for h in history},
        shares=shares,
        share_back=share_back,
        latest=latest,
        up_to_date=up_to_date or {},
        prefill=prefill,
        waiting=waiting,
        # 보드 추가 칸은 접어 둔다. 보드가 아직 없거나, QR 로 와서 코드가 채워졌거나, 기다리는 코드가 있으면 편다.
        add_open=bool(not own or prefill or waiting),
        tz=rows[-1]["tz"] if rows else reports.DEFAULT_TZ,
        weakest=reports.WEAKEST,
        table=reports.table_rows(recent),
        logs=reports.log_blocks(recent),
        plot_data=Markup(reports.plot_data(rows, board_id, nvs_total)),
    )
