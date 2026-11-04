"""등록된 적 있는 보드를 다시 등록할 때: 같은 이메일이면 예전 기록을 가져올지 묻는다."""

import secrets
from datetime import datetime, timezone

from emcp.entry import dashboard
from emcp.infra import db
from helpers import new_code

MAC = "TESTHIST0001"
SAME, OTHER = "same@test.example", "other@test.example"


def register(user, email, mac=MAC):
    """보드가 먼저 묻고 사람이 코드를 넣는 순서로 등록. 보드 번호를 돌려준다."""
    code = new_code()
    token_hash = db.boards.hash_token(secrets.token_hex(32))
    assert db.boards.board_check_in(token_hash, mac, code)[0] == "pending"
    status, device = db.boards.enter_code(user, code, email)
    assert status == "registered"
    return device


def count(sql, *args):
    with db.core.connect() as conn:
        return conn.execute(sql, args).fetchone()["n"]


def test_tokenless_reports_go_to_the_oldest_row():
    db.reports.add_reports([{"received_at": "2026-01-01T00:00:00+00:00", "mac": "TESTLEGACY01", "battery_mv": 1}])
    db.reports.add_reports([{"received_at": "2026-01-01T00:30:00+00:00", "mac": "TESTLEGACY01", "battery_mv": 2}])
    with db.core.connect() as conn:
        conn.execute("INSERT INTO devices (mac) VALUES ('TESTLEGACY01')")       # 같은 MAC 의 새 행
    db.reports.add_reports([{"received_at": "2026-01-01T01:00:00+00:00", "mac": "TESTLEGACY01", "battery_mv": 3}])
    with db.core.connect() as conn:
        rows = conn.execute("SELECT d.id, count(r.id) AS n FROM devices d LEFT JOIN reports r ON r.device_id = d.id"
                            " WHERE d.mac = 'TESTLEGACY01' GROUP BY d.id ORDER BY d.id").fetchall()
    assert [r["n"] for r in rows] == [3, 0]


def test_same_email_imports_old_history():
    first = register("user_a", SAME)
    with db.core.connect() as conn:
        conn.execute("INSERT INTO reports (device_id, received_at, battery_mv) VALUES (%s, %s, 3900)",
                     (first, datetime(2026, 10, 1, tzinfo=timezone.utc)))
        conn.execute("UPDATE devices SET name = '거실', city = '서울', lat = 37.5, lon = 127 WHERE id = %s", (first,))
    assert db.boards.forget_user("user_a") == [first]

    again = register("user_b", SAME)
    assert again != first
    history = db.boards.previous_history("user_b")
    assert [(h["device_id"], h["reports"]) for h in history] == [(again, 1)]
    page = dashboard.page.render([], None, boards=db.boards.list_devices("user_b"), history=history)
    assert "가져오기" in page and "그냥 두기" in page and "1건" in page

    assert db.boards.import_history("user_b", again)
    assert count("SELECT count(*) AS n FROM reports WHERE device_id = %s", again) == 1
    assert count("SELECT count(*) AS n FROM devices WHERE id = %s", first) == 0
    with db.core.connect() as conn:
        moved = conn.execute("SELECT name, city FROM devices WHERE id = %s", (again,)).fetchone()
    assert moved == {"name": "거실", "city": "서울"}
    assert db.boards.previous_history("user_b") == []


def test_other_email_is_not_offered_old_history():
    first = register("user_a", SAME)
    with db.core.connect() as conn:
        conn.execute("INSERT INTO reports (device_id, received_at, battery_mv) VALUES (%s, now(), 3900)", (first,))
    db.boards.forget_user("user_a")
    newcomer = register("user_c", OTHER)
    assert db.boards.previous_history("user_c") == []
    assert not db.boards.import_history("user_c", newcomer)
    assert not db.boards.import_history("user_c", first)


def test_keep_old_history_as_is():
    first = register("user_a", SAME)
    with db.core.connect() as conn:
        conn.execute("INSERT INTO reports (device_id, received_at, battery_mv) VALUES (%s, now(), 3900)", (first,))
    db.boards.forget_user("user_a")
    again = register("user_e", SAME)
    assert [h["device_id"] for h in db.boards.previous_history("user_e")] == [again]
    assert db.boards.decline_history("user_e", again)
    assert db.boards.previous_history("user_e") == []
    assert count("SELECT count(*) AS n FROM devices WHERE id = %s", first) == 1


def test_history_buttons_on_dashboard(client, accounts):
    from helpers import post_form, register as register_by_dashboard

    me = accounts.add("user_me", SAME)
    first = register("user_old", SAME, mac="TEST00000001")
    with db.core.connect() as conn:
        conn.execute("INSERT INTO reports (device_id, received_at, battery_mv) VALUES (%s, now(), 3900)", (first,))
    db.boards.forget_user("user_old")
    _, device = register_by_dashboard(client, me, "TEST00000001")
    assert post_form(client, me, "/dashboard/history", device_id=str(device), choice="import") == \
        "/dashboard?notice=imported"
    assert post_form(client, me, "/dashboard/history", device_id=str(device), choice="import") == \
        "/dashboard?notice=not_imported"
