"""/admin: 관리자만 보는 전체 보드 목록."""

from datetime import datetime, timedelta, timezone

from emcp.infra import auth, db
from helpers import register, report, sign_in

MAC = "TEST00000001"
OTHER_MAC = "TEST00000002"


def make_admin(monkeypatch, user: str) -> None:
    monkeypatch.setattr(auth, "ADMIN_USER_IDS", frozenset({user}))


def test_admin_sees_every_board_but_not_its_private_values(client, accounts, monkeypatch):
    me, other = accounts.add("user_me"), accounts.add("user_other", "other@test.example")
    register(client, me, MAC)
    token, _ = register(client, other, OTHER_MAC)
    report(client, token, OTHER_MAC, fw_version="v1.4.1", battery_mv=3852, room_c=23.7, room_rh=41.3, chip_c=31.9)
    make_admin(monkeypatch, me)
    sign_in(client, me)
    page = client.get("/admin").text
    for part in ("other@test.example", "user_me@test.example", "v1.4.1", "3852 mV", "켜짐"):
        assert part in page
    assert "최근 보고 1건" in page
    for private in (OTHER_MAC, MAC, "23.7", "41.3", "31.9", "뮌헨"):   # MAC, 실내 온습도, 칩 온도, 위치
        assert private not in page


def test_admin_page_is_not_there_for_anyone_else(client, accounts, monkeypatch):
    me = accounts.add("user_me")
    make_admin(monkeypatch, accounts.add("user_admin"))
    sign_in(client, me)
    r = client.get("/admin")
    missing = client.get("/no-such-page")
    assert r.status_code == 404 and (r.text, r.headers["content-type"]) == (missing.text, missing.headers["content-type"])


def test_nobody_is_admin_without_the_setting(client, accounts, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_USER_IDS", frozenset())
    sign_in(client, accounts.add("user_me"))
    assert client.get("/admin").status_code == 404


def test_signed_out_goes_to_sign_in(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.headers["location"] == "/auth/login"


def test_dashboard_links_to_admin_only_for_admin(client, accounts, monkeypatch):
    me, other = accounts.add("user_me"), accounts.add("user_other")
    make_admin(monkeypatch, me)
    sign_in(client, me)
    assert 'href="/admin"' in client.get("/dashboard").text
    sign_in(client, other)
    assert 'href="/admin"' not in client.get("/dashboard").text


def test_each_board_opens_its_newest_reports(client, accounts, monkeypatch):
    me = accounts.add("user_me")
    _, device = register(client, me, MAC)
    start = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    # 22건. 깨어 있던 시간은 다음 보고가 prev_awake_ms 로 알려 준다
    db.reports.add_reports([{"device_id": device, "received_at": (start + timedelta(minutes=30 * i)).isoformat(),
                             "battery_mv": 3100 + i, "wifi_attempts": 1, "prev_awake_ms": 2000 + i,
                             "prev_steps": {"connect": 900 + i}}
                            for i in range(22)])
    make_admin(monkeypatch, me)
    sign_in(client, me)
    page = client.get("/admin").text
    assert "최근 보고 20건" in page
    assert "3102" in page and "3121" in page and "3101" not in page   # 최근 20건만
    assert "2021" in page and "2002" not in page   # 한 칸 당겨 붙이고, 보이지 않는 보고의 값은 버린다
    assert "암호화 연결" in page and "921" in page and "902" not in page   # 단계도 같은 규칙
    assert 'aria-controls="reports-%d"' % device in page
