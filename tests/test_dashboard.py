"""대시보드: 로그인 확인, 화면, 이름·위치 바꾸기, QR 로 들어오기."""

from markupsafe import escape

from emcp.entry import web
from emcp.state import devices
from helpers import new_code, post_form, register, sign_in

MAC = "TEST00000001"


def test_deleted_account_is_sent_to_sign_in(client, accounts):
    sign_in(client, "user_gone")                 # WorkOS 에 없는 계정
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/auth/login"
    assert post_form(client, "user_gone", "/dashboard/release", device_id="1") == "/auth/login"


def test_workos_unreachable_keeps_people_signed_in(client, accounts):
    accounts.reachable = False
    sign_in(client, "user_me")
    assert client.get("/dashboard").status_code == 200


def test_signed_out(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.headers["location"] == "/auth/login"
    assert client.get("/dashboard/places?q=Berlin").status_code == 401


def test_page_shows_my_boards_and_waiting_codes(client, accounts):
    me = accounts.add("user_me")
    register(client, me, MAC)
    waiting = new_code()
    post_form(client, me, "/dashboard/claim", code=waiting)
    page = client.get("/dashboard?notice=released").text
    for part in ("<h2>내 보드</h2>", MAC, "등록 해제", waiting, web.NOTICES["released"]):
        assert part in page


def test_unknown_notice_is_ignored(client, accounts):
    sign_in(client, accounts.add("user_me"))
    assert "<script>alert" not in client.get("/dashboard?notice=<script>alert(1)</script>").text


def test_add_board_is_open_only_without_boards(client, accounts):
    me = accounts.add("user_me")
    sign_in(client, me)
    assert '<details class="add" open>' in client.get("/dashboard").text
    register(client, me, MAC)
    assert '<details class="add">' in client.get("/dashboard").text


def test_rename_is_escaped(client, accounts):
    me = accounts.add("user_me")
    _, device = register(client, me, MAC)
    assert post_form(client, me, "/dashboard/rename", device_id=str(device), name="<b>거실</b>") == \
        "/dashboard?notice=renamed"
    page = client.get("/dashboard").text
    assert "&lt;b&gt;거실&lt;/b&gt;" in page and "<b>거실</b>" not in page


def test_place_search(client, accounts):
    sign_in(client, accounts.add("user_me"))
    places = client.get("/dashboard/places?q=Berlin").json()
    assert places[0] == {"name": "베를린", "where": "베를린 · 독일", "lat": 52.52, "lon": 13.41}


def test_place_search_fails(client, accounts, open_meteo):
    sign_in(client, accounts.add("user_me"))
    open_meteo.down = True
    assert client.get("/dashboard/places?q=Berlin").status_code == 502


def test_location_change_reaches_the_board_copy(client, accounts):
    me = accounts.add("user_me")
    token, device = register(client, me, MAC)
    assert post_form(client, me, "/dashboard/location", device_id=str(device), name="베를린",
                     lat="52.52", lon="13.41") == "/dashboard?notice=located"
    assert next(d for d in devices._by_token.values() if d["id"] == device)["city"] == "베를린"


def test_location_out_of_range(client, accounts):
    me = accounts.add("user_me")
    _, device = register(client, me, MAC)
    assert post_form(client, me, "/dashboard/location", device_id=str(device), name="x",
                     lat="999", lon="0") == "/dashboard?notice=not_located"
    assert post_form(client, me, "/dashboard/location", device_id=str(device), name="x",
                     lat="abc", lon="0") == "/dashboard?notice=not_located"


def test_qr_when_signed_in(client, accounts):
    sign_in(client, accounts.add("user_me"))
    r = client.get("/r/abc234", follow_redirects=False)
    assert r.headers["location"] == "/dashboard?code=ABC234"


def test_qr_when_signed_out_keeps_code_through_sign_in(client, accounts):
    r = client.get("/r/ABC234", follow_redirects=False)
    assert r.headers["location"] == "/auth/login" and r.cookies.get("emcp_claim") == "ABC234"
    client.cookies.set("emcp_claim", "ABC234")
    sign_in(client, accounts.add("user_me"))
    page = client.get("/dashboard").text
    assert 'value="ABC234"' in page and str(escape(web.NOTICES["prefilled"])) in page   # 작은따옴표는 &#39; 로


def test_bad_qr_code(client):
    assert client.get("/r/0O1I", follow_redirects=False).headers["location"] == "/dashboard"
