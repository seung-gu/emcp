"""보고 저장소 — Neon Postgres (DATABASE_URL 필요).

테이블을 어떻게 나눴고 왜 그런지는 docs/data-structure.ko.md 에 있다. 테이블을 바꿀 때는 MIGRATIONS
끝에 붙이기만 한다. 서버가 처음 DB 에 붙을 때 아직 적용하지 않은 번호만 실행한다.
"""

from __future__ import annotations

import hashlib
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
                  "out_temp_min_c", "weather_code", "wind_kmh", "pop", "timezone")
# 온습도 센서 진단. 보고마다 오지만 문제를 볼 때만 쓰므로 sensor_logs 에 따로 둔다.
SENSOR_FIELDS = {"room_err": str, "room_sda": bool, "room_scl": bool, "room_ack": str,
                 "room_stuck": bool}
# 날씨를 못 받았던 때의 기록 한 건에 실리는 값과 그 타입. kind 는 server.py 가 따로 거른다.
# 온습도(chip_c, room_c, room_rh)는 4단계 펌웨어부터 실려 온다.
LOG_FIELDS = {
    "wifi_attempts": int, "reset_reason": int, "battery_mv": int, "wifi_ms": int, "rssi": int,
    "wifi_status": int, "http_code": int, "chip_c": float, "room_c": float, "room_rh": float,
}

# 등록 코드가 살아 있는 시간. 보드가 1시간 동안 5분마다 물어보는 것과 맞춘다.
CLAIM_MINUTES = 60

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
    # 2단계: 누가 어떤 보드를 보는지. 보드 하나를 여러 사람이 공유할 수 있어서 devices 와 따로 둔다.
    """
    CREATE TABLE device_access (
        device_id BIGINT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        user_id   TEXT NOT NULL,   -- WorkOS 사용자 ID
        role      TEXT NOT NULL CHECK (role IN ('owner', 'viewer')),
        PRIMARY KEY (device_id, user_id)
    )
    """,
    "CREATE INDEX device_access_user ON device_access (user_id)",
    # 3단계: 보드마다 날씨 위치. 비어 있으면 서버 기본 위치(뮌헨)로 조회한다.
    "ALTER TABLE devices ADD COLUMN city TEXT, ADD COLUMN lat DOUBLE PRECISION, ADD COLUMN lon DOUBLE PRECISION",
    # 4단계: 보드 토큰의 SHA-256 해시. 비어 있으면 아직 토큰이 없거나 주인이 등록을 해제한 보드다.
    # MAC 중복 금지(devices_mac_key)는 옛 앱이 토큰 없는 보고를 MAC 으로 찾는 동안 남겨 둔다.
    "ALTER TABLE devices ADD COLUMN token_hash TEXT UNIQUE",
    # 등록 코드 한 줄에 사람(user_id)과 보드(token_hash, mac)가 각자 자기 몫을 적는다. 둘 다 차면 연결한다.
    """
    CREATE TABLE claim_codes (
        code       TEXT PRIMARY KEY,
        user_id    TEXT,
        token_hash TEXT,
        mac        TEXT,
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    "ALTER TABLE wake_logs ADD COLUMN chip_c DOUBLE PRECISION, ADD COLUMN room_c DOUBLE PRECISION,"
    " ADD COLUMN room_rh DOUBLE PRECISION",
    # 서버가 기억해 둘 값 몇 개. 지금은 WorkOS 의 계정 삭제를 어디까지 확인했는지 하나뿐이다.
    "CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # 등록된 적 있는 보드도 다른 계정이 등록할 수 있게 MAC 중복 금지를 푼다. 그런 등록은 새 행으로 시작한다.
    # owner_email 은 등록한 사람의 이메일. 다시 등록한 사람과 같으면 예전 기록을 가져올지 묻는다.
    # history_declined 는 그 질문에 "그냥 두기" 를 고른 보드라 다시 묻지 않는다.
    "ALTER TABLE devices DROP CONSTRAINT devices_mac_key, ADD COLUMN owner_email TEXT,"
    " ADD COLUMN history_declined BOOLEAN NOT NULL DEFAULT false",
    "CREATE INDEX devices_mac ON devices (mac)",
    "ALTER TABLE claim_codes ADD COLUMN email TEXT",
    # 그 보고 때 날씨를 받은 위치의 시간대(IANA 이름, 예: Europe/Berlin). 대시보드가 시각을 현지 시각으로 보여 준다.
    "ALTER TABLE reports ADD COLUMN timezone TEXT",
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
    (received_at, device_id 또는 mac, 측정값, 날씨, room_*, log, raw). 토큰으로 온 보고는 서버가
    보드 번호(device_id)를 적어 두고, 토큰 없는 옛 보고는 MAC 으로 보드를 찾는다.

    같은 보드에 같은 받은 시각의 보고가 이미 있으면 건너뛰므로, 같은 묶음을 다시 넣어도 된다.
    넣은 보고 수, 건너뛴 보고 수, 넣은 실패 기록 수를 돌려준다.
    """
    rows = sorted(rows, key=lambda r: r["received_at"])
    report_cols = ["device_id", "received_at", *REPORT_FIELDS, *WEATHER_FIELDS]
    with _connect() as conn, conn.transaction():
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


def devices_for(conn: psycopg.Connection, user_id: str) -> list[int]:
    """이 사람이 볼 수 있는 보드 (주인이거나 공유받은 것). 사람이 보고를 읽는 조회는 모두 이걸 거친다 —
    조건을 핸들러마다 따로 쓰면 하나만 빠뜨려도 에러 없이 남의 데이터가 나간다."""
    rows = conn.execute("SELECT device_id FROM device_access WHERE user_id = %s", (user_id,)).fetchall()
    return [r["device_id"] for r in rows]


# 보드를 바꾸는 조회에 붙이는 조건: 요청한 사람이 그 보드의 주인이다.
_OWNED_BY = "id IN (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner')"


def list_devices(user_id: str) -> list[dict]:
    """이 사람이 볼 수 있는 보드의 번호, 이름, MAC, 위치, 역할, 토큰으로 등록됐는지(registered)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT d.id, d.name, d.mac, d.city, d.lat, d.lon, a.role,"
            " d.token_hash IS NOT NULL AS registered"
            " FROM devices d JOIN device_access a ON a.device_id = d.id"
            " WHERE a.user_id = %s ORDER BY d.id",
            (user_id,),
        ).fetchall()


def set_location(user_id: str, device_id: int, city: str, lat: float, lon: float) -> bool:
    """주인이면 보드의 날씨 위치를 바꾸고 True. 남의 보드와 없는 번호는 똑같이 False 다 — 그 보드가
    있는지조차 알려 주지 않는다."""
    with _connect() as conn:
        cur = conn.execute(f"UPDATE devices SET city = %s, lat = %s, lon = %s WHERE id = %s AND {_OWNED_BY}",
                           (city, lat, lon, device_id, user_id))
        return cur.rowcount == 1


def rename_device(user_id: str, device_id: int, name: str) -> bool:
    """주인이면 보드 이름을 바꾸고 True. 실패는 set_location 과 같다."""
    with _connect() as conn:
        cur = conn.execute(f"UPDATE devices SET name = %s WHERE id = %s AND {_OWNED_BY}",
                           (name, device_id, user_id))
        return cur.rowcount == 1


def read_devices() -> list[dict]:
    """모든 보드의 번호, MAC, 토큰 해시, 날씨 위치. 서버가 메모리에 사본으로 들고 있다가 보고에 답할 때
    쓴다. 보드가 몇 대 안 돼서 통째로 읽는다."""
    with _connect() as conn:
        return conn.execute("SELECT id, mac, token_hash, city, lat, lon FROM devices").fetchall()


def hash_token(token: str) -> str:
    """보드 토큰은 원본을 저장하지 않고 이 해시로만 찾는다."""
    return hashlib.sha256(token.encode()).hexdigest()


# --- 보드 등록 (4단계, docs/multi-user-rollout.ko.md) ---
# 사람과 보드가 claim_codes 의 같은 줄에 각자 자기 몫을 적고, 둘 다 차면 연결한다. 누가 먼저 오든 같다.
# 두 함수 모두 ("registered", 보드 번호), ("pending", None), ("taken", None) 중 하나를 돌려준다.

def board_check_in(token_hash: str, mac: str, code: str) -> tuple[str, int | None]:
    """보드가 등록됐는지 묻는다. 이미 등록된 토큰이면 바로 그 보드 번호를 돌려준다. taken 은 다른 보드가
    같은 코드를 이미 쓰고 있다는 뜻이라 보드가 코드를 새로 만든다."""
    with _connect() as conn, conn.transaction():
        if (row := conn.execute("SELECT id FROM devices WHERE token_hash = %s", (token_hash,)).fetchone()):
            return "registered", row["id"]
        claim = _claim_row(conn, code)
        if claim["token_hash"] not in (None, token_hash):
            return "taken", None
        conn.execute("UPDATE claim_codes SET token_hash = %s, mac = %s WHERE code = %s", (token_hash, mac, code))
        if claim["user_id"]:
            return _link(conn, code, claim["user_id"], claim["email"], token_hash, mac)
        return "pending", None


def enter_code(user_id: str, code: str, email: str | None) -> tuple[str, int | None]:
    """사람이 대시보드나 Claude 에 보드 디스플레이의 코드를 입력했다. email 은 그 사람의 WorkOS 이메일로,
    보드에 주인 이메일로 남는다. taken 은 다른 계정이 같은 코드를 이미 입력했다는 뜻이다."""
    with _connect() as conn, conn.transaction():
        claim = _claim_row(conn, code)
        if claim["user_id"] not in (None, user_id):
            return "taken", None
        conn.execute("UPDATE claim_codes SET user_id = %s, email = %s WHERE code = %s", (user_id, email, code))
        if claim["token_hash"]:
            return _link(conn, code, user_id, email, claim["token_hash"], claim["mac"])
        return "pending", None


def _claim_row(conn: psycopg.Connection, code: str) -> dict:
    """코드 줄을 잠그고 가져온다. 없으면 만든다. 만료된 줄은 그 전에 지운다."""
    conn.execute("DELETE FROM claim_codes WHERE expires_at <= now()")
    conn.execute("INSERT INTO claim_codes (code, expires_at) VALUES (%s, now() + make_interval(mins => %s))"
                 " ON CONFLICT (code) DO NOTHING", (code, CLAIM_MINUTES))
    return conn.execute("SELECT * FROM claim_codes WHERE code = %s FOR UPDATE", (code,)).fetchone()


def _link(conn: psycopg.Connection, code: str, user_id: str, email: str | None, token_hash: str,
          mac: str) -> tuple[str, int | None]:
    """코드 줄의 두 쪽이 다 찼으니 보드를 계정에 연결하고 줄을 지운다. 같은 계정이 같은 MAC 의 보드를 다시
    등록하면 새 행을 만들지 않고 그 행의 토큰만 바꿔서 기록을 잇는다. 등록된 적 있는 보드라도 다른 계정이면
    새 행으로 시작한다. 예전 기록은 주인 이메일이 같을 때만 가져올 수 있다(previous_history)."""
    row = conn.execute(
        "SELECT d.id FROM devices d JOIN device_access a ON a.device_id = d.id"
        " WHERE d.mac = %s AND a.user_id = %s AND a.role = 'owner'",
        (mac, user_id),
    ).fetchone()
    if row:
        device_id = row["id"]
        conn.execute("UPDATE devices SET token_hash = %s, owner_email = coalesce(%s, owner_email) WHERE id = %s",
                     (token_hash, email, device_id))
    else:
        device_id = conn.execute("INSERT INTO devices (mac, token_hash, owner_email) VALUES (%s, %s, %s)"
                                 " RETURNING id", (mac, token_hash, email)).fetchone()["id"]
        conn.execute("INSERT INTO device_access (device_id, user_id, role) VALUES (%s, %s, 'owner')",
                     (device_id, user_id))
    conn.execute("DELETE FROM claim_codes WHERE code = %s", (code,))
    return "registered", device_id


# 예전 기록: 같은 MAC 이고 주인 이메일이 같은, 이 보드가 아닌 행. 계정을 지웠다 다시 만든 사람의 보드다.
# 이메일이 다르면(보드를 넘겨받은 사람) 전 주인의 기록이라 보여 주지도 묻지도 않는다.
_PREVIOUS = ("SELECT o.id FROM devices o WHERE o.mac = d.mac AND o.id <> d.id"
             " AND o.owner_email IS NOT NULL AND o.owner_email = d.owner_email")


def previous_history(user_id: str) -> list[dict]:
    """이 사람이 주인인 보드마다 가져올 수 있는 예전 기록의 건수와 기간. 그냥 두기로 한 보드는 뺀다."""
    with _connect() as conn:
        return conn.execute(
            "SELECT d.id AS device_id, count(r.id) AS reports, min(r.received_at) AS first,"
            " max(r.received_at) AS last"
            " FROM devices d JOIN device_access a ON a.device_id = d.id AND a.role = 'owner'"
            f" JOIN reports r ON r.device_id IN ({_PREVIOUS})"
            " WHERE a.user_id = %s AND NOT d.history_declined"
            " GROUP BY d.id ORDER BY d.id",
            (user_id,),
        ).fetchall()


def import_history(user_id: str, device_id: int) -> bool:
    """예전 행의 보고를 이 보드로 옮기고 예전 행을 지운다. 이름과 위치가 비어 있으면 예전 것을 쓴다. 보드의
    번호와 토큰은 그대로라 보고 버퍼와 서버의 보드 사본은 그대로 맞는다. 주인이 아니거나 가져올 게 없으면
    False."""
    with _connect() as conn, conn.transaction():
        d = conn.execute(f"SELECT * FROM devices WHERE id = %s AND {_OWNED_BY}", (device_id, user_id)).fetchone()
        if not d or not d["owner_email"]:
            return False
        old = [r["id"] for r in conn.execute(
            "SELECT id FROM devices WHERE mac = %s AND id <> %s AND owner_email = %s ORDER BY id",
            (d["mac"], device_id, d["owner_email"]))]
        if not old:
            return False
        # 같은 시각의 보고가 두 행에 다 있으면(있을 리 없지만) 이 보드 것을 남기고 예전 것은 버린다.
        conn.execute("UPDATE reports r SET device_id = %s WHERE r.device_id = ANY(%s) AND NOT EXISTS"
                     " (SELECT 1 FROM reports x WHERE x.device_id = %s AND x.received_at = r.received_at)",
                     (device_id, old, device_id))
        conn.execute("DELETE FROM reports WHERE device_id = ANY(%s)", (old,))
        last = conn.execute("SELECT name, city, lat, lon FROM devices WHERE id = ANY(%s) ORDER BY id DESC LIMIT 1",
                            (old,)).fetchone()
        conn.execute("UPDATE devices SET name = coalesce(name, %s),"
                     " city = coalesce(city, %s), lat = CASE WHEN city IS NULL THEN %s ELSE lat END,"
                     " lon = CASE WHEN city IS NULL THEN %s ELSE lon END WHERE id = %s",
                     (last["name"], last["city"], last["lat"], last["lon"], device_id))
        conn.execute("DELETE FROM device_access WHERE device_id = ANY(%s)", (old,))
        conn.execute("DELETE FROM devices WHERE id = ANY(%s)", (old,))
    return True


def decline_history(user_id: str, device_id: int) -> bool:
    """예전 기록을 가져오지 않기로 했다. 다시 묻지 않는다. 예전 행은 그대로 둔다."""
    with _connect() as conn:
        cur = conn.execute(f"UPDATE devices SET history_declined = true WHERE id = %s AND {_OWNED_BY}",
                           (device_id, user_id))
        return cur.rowcount == 1


def pending_codes(user_id: str) -> list[dict]:
    """이 사람이 입력했지만 아직 보드가 물어오지 않은 코드와 만료 시각."""
    with _connect() as conn:
        return conn.execute(
            "SELECT code, expires_at FROM claim_codes WHERE user_id = %s AND expires_at > now()"
            " ORDER BY expires_at", (user_id,),
        ).fetchall()


def release_device(user_id: str, device_id: int) -> bool:
    """주인이면 보드의 토큰을 지운다. 보드는 다음 보고에서 거절당하면 토큰을 지우고 새 코드를 띄운다. 지난
    기록과 주인 정보는 그대로라 전 주인은 계속 볼 수 있다."""
    with _connect() as conn:
        cur = conn.execute(f"UPDATE devices SET token_hash = NULL WHERE id = %s AND token_hash IS NOT NULL"
                           f" AND {_OWNED_BY}", (device_id, user_id))
        return cur.rowcount == 1


def forget_user(user_id: str) -> list[int]:
    """지워진 계정을 보드에서 떼어 낸다. 그 사람이 주인인 보드는 토큰을 지워 등록을 해제하고(보드는 다음
    보고에서 거절당하면 새 코드를 띄운다), 그 사람의 공유와 입력해 둔 코드도 지운다. 지난 보고는 남긴다.
    해제한 보드 번호를 돌려준다. 두 번 불러도 결과가 같다."""
    with _connect() as conn, conn.transaction():
        released = conn.execute(
            "UPDATE devices SET token_hash = NULL WHERE token_hash IS NOT NULL AND id IN"
            " (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner') RETURNING id",
            (user_id,),
        ).fetchall()
        conn.execute("DELETE FROM device_access WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM claim_codes WHERE user_id = %s", (user_id,))
    return [r["id"] for r in released]


def get_state(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES (%s, %s)"
                     " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def read_reports(user_id: str) -> list[dict]:
    """이 사람이 볼 수 있는 보드의 보고를 받은 시각 순으로, 대시보드가 그리는 모양으로. 비어 있는 칸은
    키째 빠진다 — 옛 파일의 한 줄과 같은 모양이라 dashboard.py 가 둘을 구분하지 않아도 된다."""
    sensor_cols = ", ".join(f"s.{k}" for k in SENSOR_FIELDS)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT r.*, d.mac, d.name AS device_name, {sensor_cols},"
            "  (SELECT jsonb_agg(to_jsonb(w) - 'report_id' - 'idx' ORDER BY w.idx)"
            "     FROM wake_logs w WHERE w.report_id = r.id) AS log"
            " FROM reports r JOIN devices d ON d.id = r.device_id"
            " LEFT JOIN sensor_logs s ON s.report_id = r.id"
            " WHERE r.device_id = ANY(%s)"
            " ORDER BY r.received_at, r.id",
            (devices_for(conn, user_id),),
        ).fetchall()
    out = []
    for r in rows:
        r["received_at"] = r["received_at"].astimezone(timezone.utc).isoformat(timespec="seconds")
        if r["log"]:
            r["log"] = [{k: v for k, v in e.items() if v is not None} for e in r["log"]]
        out.append({k: v for k, v in r.items() if v is not None and k not in ("id", "device_id")})
    return out
