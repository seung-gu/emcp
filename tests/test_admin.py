"""/admin: 관리자만 보는 전체 보드 목록."""

from emcp.infra import auth
from helpers import register, report, sign_in

MAC = "TEST00000001"
OTHER_MAC = "TEST00000002"


def make_admin(monkeypatch, user: str) -> None:
    monkeypatch.setattr(auth, "ADMIN_USER_IDS", frozenset({user}))


def test_admin_sees_every_board_but_not_its_private_values(client, accounts, monkeypatch):
    me, other = accounts.add("user_me"), accounts.add("user_other", "other@test.example")
    register(client, me, MAC)
    token, _ = register(client, other, OTHER_MAC)
    report(client, token, OTHER_MAC, fw_version="v1.4.1", battery_mv=3852, room_c=23.7, room_rh=41.3)
    make_admin(monkeypatch, me)
    sign_in(client, me)
    page = client.get("/admin").text
    for part in ("other@test.example", "user_me@test.example", "v1.4.1", "3852 mV", "켜짐"):
        assert part in page
    for private in (OTHER_MAC, MAC, "23.7", "41.3", "뮌헨"):   # MAC, 실내 온습도, 위치
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
