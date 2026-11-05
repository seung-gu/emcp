"""보드가 보낸 보고: 저장(add_reports)과 대시보드가 그리는 모양으로 읽기(read_reports)."""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from .boards import devices_for
from .core import connect

# 보드가 보내는 측정값. 이름 = reports 칸 이름 = 대시보드가 받는 키이고, 값은 서버가 들어온 값을
# 거를 때 쓰는 타입이다. 새 항목은 여기 한 줄과 MIGRATIONS 의 칸 추가 한 줄이면 된다.
REPORT_FIELDS = {
    "fw_version": str,
    "battery_mv": int, "chip_c": float, "room_c": float, "room_rh": float,
    "wifi_ms": int, "rssi": int, "wifi_attempts": int,
    "reset_reason": int, "prev_awake_ms": int, "nvs_free": int, "nvs_total": int,
}
# 서버가 그 보고에 돌려준 날씨. 조회에 실패한 보고는 전부 비어 있다.
WEATHER_FIELDS = ("city", "lat", "lon", "out_temp_c", "out_humidity", "out_temp_max_c",
                  "out_temp_min_c", "weather_code", "wind_kmh", "pop", "timezone")
# 온습도 센서 진단. 보고마다 오지만 문제를 볼 때만 쓰므로 sensor_logs 에 따로 둔다.
SENSOR_FIELDS = {"room_err": str, "room_sda": bool, "room_scl": bool, "room_ack": str,
                 "room_stuck": bool}
# 날씨를 못 받았던 때의 기록 한 건에 실리는 값과 그 타입. kind 는 checks.py 가 따로 거른다.
# 온습도(chip_c, room_c, room_rh)는 4단계 펌웨어부터 실려 온다.
LOG_FIELDS = {
    "wifi_attempts": int, "reset_reason": int, "battery_mv": int, "wifi_ms": int, "rssi": int,
    "wifi_status": int, "http_code": int, "chip_c": float, "room_c": float, "room_rh": float,
}


def add_reports(rows: list[dict]) -> dict:
    """보고를 한 트랜잭션으로 넣는다. rows 는 board_api.py 가 버퍼에 쓰는 모양이다
    (received_at, device_id 또는 mac, 측정값, 날씨, room_*, log, raw). 토큰으로 온 보고는 서버가
    보드 번호(device_id)를 적어 두고, 토큰 없는 옛 보고는 MAC 으로 보드를 찾는다.

    같은 보드에 같은 받은 시각의 보고가 이미 있으면 건너뛰므로, 같은 묶음을 다시 넣어도 된다.
    넣은 보고 수, 건너뛴 보고 수, 넣은 실패 기록 수를 돌려준다.
    """
    rows = sorted(rows, key=lambda r: r["received_at"])
    report_cols = ["device_id", "received_at", *REPORT_FIELDS, *WEATHER_FIELDS]
    with connect() as conn, conn.transaction():
        by_mac = _device_ids(conn, {r.get("mac") for r in rows if "device_id" not in r})
        # rows 와 같은 순서로 각 보고의 보드 번호
        row_devices = [r["device_id"] if "device_id" in r else by_mac[r.get("mac")] for r in rows]
        sent_before = _last_sent_logs(conn, list(set(row_devices)))

        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO reports ({', '.join(report_cols)})"
                f" VALUES ({', '.join(['%s'] * len(report_cols))})"
                " ON CONFLICT (device_id, received_at) DO NOTHING RETURNING id",
                [(device, datetime.fromisoformat(r["received_at"]),
                  *(r.get(c) for c in report_cols[2:])) for r, device in zip(rows, row_devices)],
                returning=True,
            )
            ids = []
            while True:
                hit = cur.fetchone()
                ids.append(hit["id"] if hit else None)  # 이미 있던 보고는 아무것도 돌아오지 않는다
                if not cur.nextset():
                    break

        raws, sensors, wakes = [], [], []
        for r, device, report_id in zip(rows, row_devices, ids):
            entries = _new_entries(r, sent_before.get(device, []))
            sent_before[device] = _sent_log(r)
            if report_id is None:
                continue
            if r.get("raw") is not None:
                raws.append((report_id, Jsonb(r["raw"])))
            if any(k in r for k in SENSOR_FIELDS):
                sensors.append((report_id, *(r.get(k) for k in SENSOR_FIELDS)))
            wakes += [(report_id, i, e.get("kind"), *(e.get(k) for k in LOG_FIELDS))
                      for i, e in enumerate(entries)]

        with conn.cursor() as cur:
            if raws:
                cur.executemany("INSERT INTO report_raw (report_id, body) VALUES (%s, %s)", raws)
            if sensors:
                cur.executemany(
                    f"INSERT INTO sensor_logs (report_id, {', '.join(SENSOR_FIELDS)})"
                    f" VALUES ({', '.join(['%s'] * (1 + len(SENSOR_FIELDS)))})", sensors)
            if wakes:
                cur.executemany(
                    f"INSERT INTO wake_logs (report_id, idx, kind, {', '.join(LOG_FIELDS)})"
                    f" VALUES ({', '.join(['%s'] * (3 + len(LOG_FIELDS)))})", wakes)

    added = sum(i is not None for i in ids)
    return {"reports": added, "skipped": len(rows) - added, "wake_logs": len(wakes)}


def _device_ids(conn: psycopg.Connection, macs: set) -> dict:
    """토큰 없는 옛 보고의 MAC 마다 devices 행 번호. 처음 보는 MAC 이면 행을 만든다. MAC 이 없는 보고는 MAC
    빈 행 하나에 모인다. 같은 MAC 의 행이 여럿이면(다른 계정이 다시 등록한 보드) 가장 오래된 행으로 간다.
    옛 펌웨어로 보고하는 건 다시 등록되기 전의 그 보드다. 옛 보고는 emcp-led 한 앱만 받으므로 두 앱이 같은
    MAC 의 행을 동시에 만들 일은 없다."""
    ids = {}
    for mac in macs:
        row = conn.execute("SELECT id FROM devices WHERE mac IS NOT DISTINCT FROM %s ORDER BY id LIMIT 1",
                           (mac,)).fetchone()
        if row is None:
            row = conn.execute("INSERT INTO devices (mac) VALUES (%s) RETURNING id", (mac,)).fetchone()
        ids[mac] = row["id"]
    return ids


def _last_sent_logs(conn: psycopg.Connection, device_ids: list) -> dict:
    """보드마다 가장 최근 보고에 실려 왔던 실패 기록 목록 (원본 그대로). 원본이 없는 옛 보고는 모른다."""
    rows = conn.execute(
        "SELECT DISTINCT ON (r.device_id) r.device_id, rr.body -> 'log' AS log"
        " FROM reports r JOIN report_raw rr ON rr.report_id = r.id"
        " WHERE r.device_id = ANY(%s) ORDER BY r.device_id, r.received_at DESC",
        (device_ids,),
    ).fetchall()
    return {r["device_id"]: r["log"] or [] for r in rows}


def _sent_log(row: dict) -> list:
    """보드가 실제로 보낸 실패 기록 목록. 원본이 있으면 원본, 옛 파일에서 옮긴 보고면 거른 목록이다.
    앞뒤 보고를 비교할 때 같은 모양끼리 비교하려고 쓴다."""
    raw = row.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("log"), list):
        return raw["log"]
    return row.get("log") or []


def _new_entries(row: dict, before: list) -> list:
    """이 보고의 실패 기록 중 새로 저장할 것.

    보드는 200 을 받아야 기록을 지운다. 앞 보고가 날씨를 못 받았으면(500 등) 보드는 그 보고까지
    실패로 적고, 앞 보고에 실었던 기록을 다시 싣는다. 그래서 이번 기록이 앞 보고의 기록으로 시작하고
    바로 다음이 http 실패(앞 보고 자신)면 그 앞부분은 이미 넣은 것이라 뺀다. 날씨 조회 실패(500)는
    서버가 받아 둔 보고에 대한 기록이라 reports 의 빈 날씨 칸으로 알 수 있으므로 넣지 않는다.
    """
    entries = row.get("log") or []
    sent = _sent_log(row)
    if before and sent[:len(before)] == before and len(sent) > len(before):
        nxt = sent[len(before)]
        if isinstance(nxt, dict) and nxt.get("kind") == "http":
            entries = entries[len(before):]
    return [e for e in entries if not (e.get("kind") == "http" and e.get("http_code") == 500)]


def recent_reports(user_id: str, device_id: int, since: datetime) -> list[dict] | None:
    """이 사람이 볼 수 있는 보드 하나의 since 이후 보고, 받은 시각 순. Claude 의 get_reports 가 쓴다. 볼 수 없는
    보드면 None 이다. 남의 보드와 없는 번호를 구분하지 않는다."""
    with connect() as conn:
        if device_id not in devices_for(conn, user_id):
            return None
        return conn.execute(
            "SELECT r.received_at, r.fw_version, r.battery_mv, r.rssi, r.wifi_ms, r.chip_c, r.room_c, r.room_rh,"
            " r.reset_reason, r.timezone,"
            " (SELECT count(*) FROM wake_logs w WHERE w.report_id = r.id) AS logged"
            " FROM reports r WHERE r.device_id = %s AND r.received_at >= %s ORDER BY r.received_at",
            (device_id, since),
        ).fetchall()


def read_reports(user_id: str) -> list[dict]:
    """이 사람이 볼 수 있는 보드의 보고를 받은 시각 순으로, 대시보드가 그리는 모양으로. 보드는 device_id 로
    구분하고, MAC 은 내가 주인인 보드에만 붙인다(공유받은 사람에게는 이름만 보인다). 비어 있는 칸은
    키째 빠진다 — 옛 파일의 한 줄과 같은 모양이라 dashboard/ 가 둘을 구분하지 않아도 된다."""
    sensor_cols = ", ".join(f"s.{k}" for k in SENSOR_FIELDS)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT r.*, d.name AS device_name, {sensor_cols},"
            "  CASE WHEN EXISTS (SELECT 1 FROM device_access o WHERE o.device_id = d.id AND o.user_id = %s"
            "    AND o.role = 'owner') THEN d.mac END AS mac,"
            "  (SELECT jsonb_agg(to_jsonb(w) - 'report_id' - 'idx' ORDER BY w.idx)"
            "     FROM wake_logs w WHERE w.report_id = r.id) AS log"
            " FROM reports r JOIN devices d ON d.id = r.device_id"
            " LEFT JOIN sensor_logs s ON s.report_id = r.id"
            " WHERE r.device_id = ANY(%s)"
            " ORDER BY r.received_at, r.id",
            (user_id, devices_for(conn, user_id)),
        ).fetchall()
    out = []
    for r in rows:
        r["received_at"] = r["received_at"].astimezone(timezone.utc).isoformat(timespec="seconds")
        if r["log"]:
            r["log"] = [{k: v for k, v in e.items() if v is not None} for e in r["log"]]
        out.append({k: v for k, v in r.items() if v is not None and k != "id"})
    return out
