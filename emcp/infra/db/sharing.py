"""공유. 주인만 공유하고 취소한다. 공유받은 사람(viewer)은 보고를 보기만 하고 보드를 바꾸지 못한다.
수락할 때 받는 사람은 자기 보드 중 요청한 주인에게도 보여 줄 것을 고른다. 수락한 공유는 어느 한쪽이 끊으면
두 사람 사이의 공유가 양쪽 다 끊긴다(_cut)."""

from __future__ import annotations

import psycopg

from .boards import OWNED_BY
from .core import connect


def _owner(conn: psycopg.Connection, device_id: int) -> dict | None:
    """보드 주인의 사용자 ID 와 이메일."""
    return conn.execute("SELECT a.user_id, coalesce(a.email, d.owner_email) AS email FROM device_access a"
                        " JOIN devices d ON d.id = a.device_id WHERE a.device_id = %s AND a.role = 'owner'",
                        (device_id,)).fetchone()


def _cut(conn: psycopg.Connection, a: str, b: str) -> None:
    """두 사람 사이의 공유를 양쪽 다 지운다: 한 사람이 주인인 보드에 붙은 다른 사람의 줄(요청 중인 것도)."""
    conn.execute(
        "DELETE FROM device_access v WHERE v.role = 'viewer' AND ("
        " (v.user_id = %s AND v.device_id IN (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner'))"
        " OR (v.user_id = %s AND v.device_id IN (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner')))",
        (b, a, a, b))


def share_device(owner_id: str, device_id: int, viewer_id: str, email: str) -> bool:
    """주인이면 그 계정에 공유를 요청한다. 받는 사람이 수락하기 전에는 아무것도 보이지 않는다. 이미 요청했거나
    공유 중이면 그대로 둔다."""
    with connect() as conn:
        if not conn.execute(f"SELECT 1 FROM devices WHERE id = %s AND {OWNED_BY}", (device_id, owner_id)).fetchone():
            return False
        conn.execute("INSERT INTO device_access (device_id, user_id, role, accepted, email)"
                     " VALUES (%s, %s, 'viewer', false, %s) ON CONFLICT DO NOTHING", (device_id, viewer_id, email))
        return True


def incoming_shares(user_id: str) -> list[dict]:
    """이 사람에게 온, 아직 수락하지 않은 공유 요청. 보드 이름과 요청한 주인의 이메일. MAC 은 주지 않는다."""
    with connect() as conn:
        return conn.execute(
            "SELECT d.id, d.name, coalesce(o.email, d.owner_email) AS owner_email FROM device_access a"
            " JOIN devices d ON d.id = a.device_id"
            " LEFT JOIN device_access o ON o.device_id = a.device_id AND o.role = 'owner'"
            " WHERE a.user_id = %s AND a.role = 'viewer' AND NOT a.accepted ORDER BY d.id",
            (user_id,),
        ).fetchall()


def answer_share(user_id: str, device_id: int, accept: bool, share_back: list[int] = ()) -> bool:
    """받은 공유 요청을 수락하거나 거절한다. 수락할 때 고른 내 보드(share_back)는 요청한 주인에게 바로 보인다.
    내가 주인이 아닌 번호는 건너뛴다. 그 뒤에 등록한 보드는 따로 공유해야 보인다. 거절하면 요청이 사라진다."""
    with connect() as conn, conn.transaction():
        if not accept:
            cur = conn.execute("DELETE FROM device_access WHERE device_id = %s AND user_id = %s"
                               " AND role = 'viewer' AND NOT accepted", (device_id, user_id))
            return cur.rowcount == 1
        cur = conn.execute("UPDATE device_access SET accepted = true WHERE device_id = %s AND user_id = %s"
                           " AND role = 'viewer' AND NOT accepted", (device_id, user_id))
        if cur.rowcount != 1:
            return False
        if owner := _owner(conn, device_id):
            conn.execute("INSERT INTO device_access (device_id, user_id, role, accepted, email)"
                         " SELECT device_id, %s, 'viewer', true, %s FROM device_access"
                         " WHERE user_id = %s AND role = 'owner' AND device_id = ANY(%s)"
                         " ON CONFLICT (device_id, user_id) DO UPDATE SET accepted = true",
                         (owner["user_id"], owner["email"], user_id, list(share_back)))
    return True


def leave_share(user_id: str, device_id: int) -> bool:
    """공유받던 보드를 내 목록에서 뺀다. 그 보드 주인과의 공유가 양쪽 다 끊긴다. 주인 보드는 뺄 수 없다."""
    with connect() as conn, conn.transaction():
        cur = conn.execute("DELETE FROM device_access WHERE device_id = %s AND user_id = %s AND role = 'viewer'",
                           (device_id, user_id))
        if cur.rowcount != 1:
            return False
        if owner := _owner(conn, device_id):
            _cut(conn, owner["user_id"], user_id)
    return True


def unshare_device(owner_id: str, device_id: int, email: str) -> bool:
    """주인이면 그 이메일 사람의 보기 권한을 지운다. 수락한 공유였으면 두 사람 사이의 공유가 양쪽 다 끊긴다.
    아직 수락 전인 요청은 그 요청만 지운다."""
    with connect() as conn, conn.transaction():
        if not conn.execute(f"SELECT 1 FROM devices WHERE id = %s AND {OWNED_BY}", (device_id, owner_id)).fetchone():
            return False
        gone = conn.execute("DELETE FROM device_access WHERE device_id = %s AND role = 'viewer' AND email = %s"
                            " RETURNING user_id, accepted", (device_id, email)).fetchall()
        for v in gone:
            if v["accepted"]:
                _cut(conn, owner_id, v["user_id"])
        return True


def shares_of(owner_id: str) -> dict[int, list[dict]]:
    """주인인 보드마다 공유한 사람의 이메일과 수락했는지."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.device_id, a.email, a.accepted FROM device_access a"
            " WHERE a.role = 'viewer' AND a.device_id IN"
            " (SELECT device_id FROM device_access WHERE user_id = %s AND role = 'owner') ORDER BY 1, 2",
            (owner_id,)).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["device_id"], []).append({"email": r["email"], "accepted": r["accepted"]})
    return out
