"""보고 저장소 — Neon Postgres (DATABASE_URL 필요).

테이블을 어떻게 나눴고 왜 그런지는 docs/data-structure.ko.md 에 있다. 테이블을 바꿀 때는 MIGRATIONS
끝에 붙이기만 한다. 서버가 처음 DB 에 붙을 때 아직 적용하지 않은 번호만 실행한다.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
                  "out_temp_min_c", "weather_code", "wind_kmh", "pop")
# 온습도 센서 진단. 보고마다 오지만 문제를 볼 때만 쓰므로 sensor_logs 에 따로 둔다.
SENSOR_FIELDS = {"room_err": str, "room_sda": bool, "room_scl": bool, "room_ack": str,
                 "room_stuck": bool}
# 날씨를 못 받았던 때의 기록 한 건에 실리는 값. 모두 정수다. kind 는 server.py 가 따로 거른다.
LOG_FIELDS = ("wifi_attempts", "reset_reason", "battery_mv", "wifi_ms", "rssi", "wifi_status",
              "http_code")

# 순서대로 한 번씩만 실행된다. 이미 적용된 줄은 고치지 말고, 바꿀 게 있으면 끝에 새 줄을 붙인다.
MIGRATIONS = [
    """
    CREATE TABLE devices (
        id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        mac        TEXT,
        name       TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        -- 토큰이 들어오기 전(1~3단계)에는 MAC 으로 보드를 찾는다. MAC 을 보내지 않는 보드도 한 행에 모인다.
        CONSTRAINT devices_mac_key UNIQUE NULLS NOT DISTINCT (mac)
    )
    """,
    """
    CREATE TABLE reports (
        id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        device_id      BIGINT NOT NULL REFERENCES devices(id),
        received_at    TIMESTAMPTZ NOT NULL,
        fw_version     TEXT,
        -- 보드 측정값
        battery_mv     INT,
        chip_c         DOUBLE PRECISION,
        room_c         DOUBLE PRECISION,
        room_rh        DOUBLE PRECISION,
        -- 연결
        wifi_ms        INT,
        rssi           INT,
        wifi_attempts  INT,
        -- 보드 상태
        reset_reason   INT,
        prev_awake_ms  INT,
        nvs_free       INT,
        nvs_total      INT,
        -- 날씨: 서버가 이 보고에 돌려준 값. 조회에 실패했거나 Neon 으로 옮기기 전 보고는 비어 있다
        city           TEXT,
        lat            DOUBLE PRECISION,
        lon            DOUBLE PRECISION,
        out_temp_c     DOUBLE PRECISION,
        out_humidity   INT,
        out_temp_max_c DOUBLE PRECISION,
        out_temp_min_c DOUBLE PRECISION,
        weather_code   INT,
        wind_kmh       DOUBLE PRECISION,
        pop            INT,
        UNIQUE (device_id, received_at)
    )
    """,
    """
    CREATE TABLE report_raw (
        report_id BIGINT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
        body      JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE sensor_logs (
        report_id  BIGINT PRIMARY KEY REFERENCES reports(id) ON DELETE CASCADE,
        room_err   TEXT,
        room_sda   BOOLEAN,
        room_scl   BOOLEAN,
        room_ack   TEXT,
        room_stuck BOOLEAN
    )
    """,
    """
    CREATE TABLE wake_logs (
        report_id     BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
        idx           INT NOT NULL,
        kind          TEXT NOT NULL,
        wifi_attempts INT,
        reset_reason  INT,
        battery_mv    INT,
        wifi_ms       INT,
        rssi          INT,
        wifi_status   INT,
        http_code     INT,
        PRIMARY KEY (report_id, idx)
    )
    """,
    # 칸이 많은 reports 를 묶음별로 보는 창. 데이터는 따로 저장하지 않는다.
    """
    CREATE VIEW report_power AS
    SELECT r.id, d.mac, r.received_at, r.battery_mv, r.chip_c, r.room_c, r.room_rh
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    """
    CREATE VIEW report_network AS
    SELECT r.id, d.mac, r.received_at, r.wifi_ms, r.rssi, r.wifi_attempts
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    """
    CREATE VIEW report_status AS
    SELECT r.id, d.mac, r.received_at, r.reset_reason, r.prev_awake_ms, r.nvs_free, r.nvs_total,
           r.fw_version
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
    """
    CREATE VIEW report_weather AS
    SELECT r.id, d.mac, r.received_at, r.city, r.lat, r.lon, r.out_temp_c, r.out_humidity,
           r.out_temp_max_c, r.out_temp_min_c, r.weather_code, r.wind_kmh, r.pop,
           r.room_c, r.room_rh
    FROM reports r JOIN devices d ON d.id = r.device_id
    """,
]

_migrated = False


def _connect() -> psycopg.Connection:
    global _migrated
    # prepare_threshold=None: Neon 의 pooled 주소(PgBouncer) 뒤에서는 연결이 요청마다 바뀔 수 있어서
    # 서버 쪽에 준비해 둔 문장을 믿을 수 없다.
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, prepare_threshold=None)
    if not _migrated:
        _migrate(conn)
        _migrated = True
    return conn


def _migrate(conn: psycopg.Connection) -> None:
    """MIGRATIONS 중 아직 적용하지 않은 것만 실행한다. 앱 두 개(emcp, emcp-led)가 같은 DB 를 쓰므로
    동시에 켜져도 한쪽만 적용하도록 트랜잭션 잠금을 건다. PgBouncer 뒤에서는 세션 잠금이 풀릴 수 있다."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7311)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        done = conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_migrations").fetchone()["v"]
        for version, statement in enumerate(MIGRATIONS[done:], start=done + 1):
            conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))


def add_reports(rows: list[dict]) -> dict:
    """보고를 한 트랜잭션으로 넣는다. rows 는 server.py 가 버퍼에 쓰는 모양이다
    (received_at, mac, 측정값, 날씨, room_*, log, raw).

    같은 보드에 같은 받은 시각의 보고가 이미 있으면 건너뛰므로, 같은 묶음을 다시 넣어도 된다.
    넣은 보고 수, 건너뛴 보고 수, 넣은 실패 기록 수를 돌려준다.
    """
    rows = sorted(rows, key=lambda r: r["received_at"])
    report_cols = ["device_id", "received_at", *REPORT_FIELDS, *WEATHER_FIELDS]
    with _connect() as conn, conn.transaction():
        devices = _device_ids(conn, {r.get("mac") for r in rows})
        sent_before = _last_sent_logs(conn, list(devices.values()))

        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO reports ({', '.join(report_cols)})"
                f" VALUES ({', '.join(['%s'] * len(report_cols))})"
                " ON CONFLICT (device_id, received_at) DO NOTHING RETURNING id",
                [(devices[r.get("mac")], datetime.fromisoformat(r["received_at"]),
                  *(r.get(c) for c in report_cols[2:])) for r in rows],
                returning=True,
            )
            ids = []
            while True:
                hit = cur.fetchone()
                ids.append(hit["id"] if hit else None)  # 이미 있던 보고는 아무것도 돌아오지 않는다
                if not cur.nextset():
                    break

        raws, sensors, wakes = [], [], []
        for r, report_id in zip(rows, ids):
            device = devices[r.get("mac")]
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
    """MAC 마다 devices 행 번호. 처음 보는 MAC 이면 행을 만든다. MAC 이 없는 보고는 None 한 행에 모인다."""
    ids = {}
    for mac in macs:
        conn.execute("INSERT INTO devices (mac) VALUES (%s) ON CONFLICT (mac) DO NOTHING", (mac,))
        ids[mac] = conn.execute("SELECT id FROM devices WHERE mac IS NOT DISTINCT FROM %s",
                                (mac,)).fetchone()["id"]
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


def read_reports() -> list[dict]:
    """모든 보고를 받은 시각 순으로, 대시보드가 그리는 모양으로. 비어 있는 칸은 키째 빠진다 —
    옛 파일의 한 줄과 같은 모양이라 dashboard.py 가 둘을 구분하지 않아도 된다."""
    sensor_cols = ", ".join(f"s.{k}" for k in SENSOR_FIELDS)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT r.*, d.mac, {sensor_cols},"
            "  (SELECT jsonb_agg(to_jsonb(w) - 'report_id' - 'idx' ORDER BY w.idx)"
            "     FROM wake_logs w WHERE w.report_id = r.id) AS log"
            " FROM reports r JOIN devices d ON d.id = r.device_id"
            " LEFT JOIN sensor_logs s ON s.report_id = r.id"
            " ORDER BY r.received_at, r.id"
        ).fetchall()
    out = []
    for r in rows:
        r["received_at"] = r["received_at"].astimezone(timezone.utc).isoformat(timespec="seconds")
        if r["log"]:
            r["log"] = [{k: v for k, v in e.items() if v is not None} for e in r["log"]]
        out.append({k: v for k, v in r.items() if v is not None and k not in ("id", "device_id")})
    return out
