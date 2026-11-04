"""보드: 누가 볼 수 있는지, 이름과 날씨 위치, 등록(등록 코드), 등록 해제, 예전 기록, 지운 계정."""

from __future__ import annotations

import hashlib

import psycopg

from db.core import connect

# 등록 코드 줄이 살아 있는 시간. 사람이 코드를 넣고 1시간 안에 보드가 물어오면(RESET) 연결된다. 보드 쪽 코드는
# 등록될 때까지 그대로라, 지나면 같은 코드를 다시 넣으면 된다.
CLAIM_MINUTES = 60


def devices_for(conn: psycopg.Connection, user_id: str) -> list[int]:
    """이 사람이 볼 수 있는 보드 (주인이거나 공유받은 것). 사람이 보고를 읽는 조회는 모두 이걸 거친다 —
    조건을 핸들러마다 따로 쓰면 하나만 빠뜨려도 에러 없이 남의 데이터가 나간다."""
    rows = conn.execute("SELECT device_id FROM device_access WHERE user_id = %s AND accepted",
                        (user_id,)).fetchall()
    return [r["device_id"] for r in rows]


# 보드를 바꾸는 조회에 붙이는 조건: 요청한 사람이 그 보드의 주인이다.
OWNED_BY = "id IN (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner')"


def list_devices(user_id: str) -> list[dict]:
    """이 사람이 볼 수 있는 보드의 번호, 이름, MAC, 위치, 역할, 토큰으로 등록됐는지(registered), 주인의
    이메일(owner_email, 공유받은 보드에 주인을 보여 줄 때 씀)."""
    with connect() as conn:
        return conn.execute(
            "SELECT d.id, d.name, d.mac, d.city, d.lat, d.lon, a.role,"
            " d.token_hash IS NOT NULL AS registered, coalesce(o.email, d.owner_email) AS owner_email"
            " FROM devices d JOIN device_access a ON a.device_id = d.id"
            " LEFT JOIN device_access o ON o.device_id = d.id AND o.role = 'owner'"
            " WHERE a.user_id = %s AND a.accepted ORDER BY d.id",
            (user_id,),
        ).fetchall()


def set_location(user_id: str, device_id: int, city: str, lat: float, lon: float) -> bool:
    """주인이면 보드의 날씨 위치를 바꾸고 True. 남의 보드와 없는 번호는 똑같이 False 다 — 그 보드가
    있는지조차 알려 주지 않는다."""
    with connect() as conn:
        cur = conn.execute(f"UPDATE devices SET city = %s, lat = %s, lon = %s WHERE id = %s AND {OWNED_BY}",
                           (city, lat, lon, device_id, user_id))
        return cur.rowcount == 1


def rename_device(user_id: str, device_id: int, name: str) -> bool:
    """주인이면 보드 이름을 바꾸고 True. 실패는 set_location 과 같다."""
    with connect() as conn:
        cur = conn.execute(f"UPDATE devices SET name = %s WHERE id = %s AND {OWNED_BY}",
                           (name, device_id, user_id))
        return cur.rowcount == 1


def read_devices() -> list[dict]:
    """모든 보드의 번호, MAC, 토큰 해시, 날씨 위치. 서버가 메모리에 사본으로 들고 있다가 보고에 답할 때
    쓴다. 보드가 몇 대 안 돼서 통째로 읽는다."""
    with connect() as conn:
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
    with connect() as conn, conn.transaction():
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
    with connect() as conn, conn.transaction():
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
        conn.execute("UPDATE device_access SET email = coalesce(%s, email) WHERE device_id = %s AND user_id = %s",
                     (email, device_id, user_id))
    else:
        device_id = conn.execute("INSERT INTO devices (mac, token_hash, owner_email) VALUES (%s, %s, %s)"
                                 " RETURNING id", (mac, token_hash, email)).fetchone()["id"]
        conn.execute("INSERT INTO device_access (device_id, user_id, role, email) VALUES (%s, %s, 'owner', %s)",
                     (device_id, user_id, email))
    conn.execute("DELETE FROM claim_codes WHERE code = %s", (code,))
    return "registered", device_id


# 예전 기록: 같은 MAC 이고 주인 이메일이 같은, 이 보드가 아닌 행. 계정을 지웠다 다시 만든 사람의 보드다.
# 이메일이 다르면(보드를 넘겨받은 사람) 전 주인의 기록이라 보여 주지도 묻지도 않는다.
_PREVIOUS = ("SELECT o.id FROM devices o WHERE o.mac = d.mac AND o.id <> d.id"
             " AND o.owner_email IS NOT NULL AND o.owner_email = d.owner_email")


def previous_history(user_id: str) -> list[dict]:
    """이 사람이 주인인 보드마다 가져올 수 있는 예전 기록의 건수와 기간. 그냥 두기로 한 보드는 뺀다."""
    with connect() as conn:
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
    with connect() as conn, conn.transaction():
        d = conn.execute(f"SELECT * FROM devices WHERE id = %s AND {OWNED_BY}", (device_id, user_id)).fetchone()
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
    with connect() as conn:
        cur = conn.execute(f"UPDATE devices SET history_declined = true WHERE id = %s AND {OWNED_BY}",
                           (device_id, user_id))
        return cur.rowcount == 1


def pending_codes(user_id: str) -> list[dict]:
    """이 사람이 입력했지만 아직 보드가 물어오지 않은 코드와 만료 시각."""
    with connect() as conn:
        return conn.execute(
            "SELECT code, expires_at FROM claim_codes WHERE user_id = %s AND expires_at > now()"
            " ORDER BY expires_at", (user_id,),
        ).fetchall()


def release_device(user_id: str, device_id: int) -> bool:
    """주인이면 보드의 토큰을 지운다. 보드는 다음 보고에서 거절당하면 토큰을 지우고 새 코드를 띄운다. 지난
    기록과 주인 정보는 그대로라 전 주인은 계속 볼 수 있다."""
    with connect() as conn:
        cur = conn.execute(f"UPDATE devices SET token_hash = NULL WHERE id = %s AND token_hash IS NOT NULL"
                           f" AND {OWNED_BY}", (device_id, user_id))
        return cur.rowcount == 1


def forget_user(user_id: str) -> list[int]:
    """지워진 계정을 보드에서 떼어 낸다. 그 사람이 주인인 보드는 토큰을 지워 등록을 해제하고(보드는 다음
    보고에서 거절당하면 새 코드를 띄운다), 그 사람의 공유와 입력해 둔 코드도 지운다. 지난 보고는 남긴다.
    해제한 보드 번호를 돌려준다. 두 번 불러도 결과가 같다."""
    with connect() as conn, conn.transaction():
        released = conn.execute(
            "UPDATE devices SET token_hash = NULL WHERE token_hash IS NOT NULL AND id IN"
            " (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner') RETURNING id",
            (user_id,),
        ).fetchall()
        conn.execute("DELETE FROM device_access WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM claim_codes WHERE user_id = %s", (user_id,))
    return [r["id"] for r in released]
