"""대시보드: 로그인 확인, 화면, 이름·위치 바꾸기, QR 로 들어오기."""

from markupsafe import escape

from emcp.entry import web
from emcp.entry.dashboard import reports as dashboard_reports
from emcp.infra import db
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


def test_cancel_a_waiting_code(client, accounts):
    me, other = accounts.add("user_me"), accounts.add("user_other")
    waiting = new_code()
    post_form(client, me, "/dashboard/claim", code=waiting)
    assert 'action="/dashboard/cancel-code"' in client.get("/dashboard").text
    assert post_form(client, other, "/dashboard/cancel-code", code=waiting) == "/dashboard?notice=not_cancelled"
    assert post_form(client, me, "/dashboard/cancel-code", code=waiting.lower()) == "/dashboard?notice=code_cancelled"
    assert waiting not in client.get("/dashboard").text


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


def test_firmware_column_compares_the_board_with_the_latest_release(client, accounts, releases):
    from emcp.infra import firmware
    from helpers import report
    releases.publish(["c" * 40, "b" * 40], {"weather-xiao.bin": b"\xe9ccccccc"}, tag="v1.04")
    firmware.refresh()
    me = accounts.add("user_me")
    token, device = register(client, me, MAC)
    sign_in(client, me)
    assert "기기 버전" in client.get("/dashboard").text and "보고 없음" in client.get("/dashboard").text

    report(client, token, MAC, fw_board="weather-xiao", fw_version="0ad6b72")
    page = client.get("/dashboard").text
    assert "0ad6b72" in page and "v1.04" in page and "다음 보고 때 v1.04 설치" in page

    post_form(client, me, "/dashboard/auto-update", device_id=str(device))
    page = client.get("/dashboard").text
    assert "자동 업데이트 꺼짐 · 최신 버전과 다름" in page and web.NOTICES["auto_off"] in client.get(
        "/dashboard?notice=auto_off").text



def test_firmware_column_says_when_the_board_is_up_to_date(client, accounts, releases):
    """보고는 초 단위로 한 줄이라, 버전이 다른 두 보고를 한 테스트에서 보내지 않는다."""
    from emcp.infra import firmware
    from helpers import report
    releases.publish(["c" * 40, "b" * 40], {"weather-xiao.bin": b"\xe9ccccccc"}, tag="v1.04")
    firmware.refresh()
    me = accounts.add("user_me")
    token, _ = register(client, me, MAC)
    report(client, token, MAC, fw_board="weather-xiao", fw_version="v1.04", fw_commit="c" * 7)
    sign_in(client, me)
    assert "✓ 최신 버전입니다" in client.get("/dashboard").text


def test_only_the_owner_changes_auto_update(client, accounts):
    _, device = register(client, accounts.add("user_owner"), MAC)
    other = accounts.add("user_other")
    assert post_form(client, other, "/dashboard/auto-update", device_id=str(device)) == "/dashboard?notice=not_auto"
    assert devices.find_by_mac(MAC)["auto_update"]


def test_page_has_a_dark_mode_switch(client, accounts):
    sign_in(client, accounts.add("user_me"))
    assert 'id="dark-mode"' in client.get("/dashboard").text


def test_radio_time_lands_on_the_wake_it_measured(client, accounts):
    me = accounts.add("user_me")
    _, device = register(client, me, MAC)
    # 10:30 보고에 실려 온 값은 10:00 wake 를 잰 것이라 10:00 행에 붙는다. 10:00 보고의 값(401)은 그 앞 wake 의 것인데
    # 그 행이 없으니 어디에도 안 보인다.
    db.reports.add_reports([
        {"received_at": "2026-11-11T10:00:00+00:00", "device_id": device, "wifi_ms": 64, "rssi": -55, "wifi_attempts": 1,
         "prev_awake_ms": 1901, "prev_radio_ms": 401},
        {"received_at": "2026-11-11T10:30:00+00:00", "device_id": device, "wifi_ms": 64, "rssi": -55, "wifi_attempts": 1,
         "prev_awake_ms": 2402, "prev_radio_ms": 913},
    ])
    sign_in(client, me)
    page = client.get("/dashboard").text
    assert '<td>2402</td><td class="step">913</td>' in page   # 깨어 있던 ms 바로 뒤, 접혀 있는 칸
    assert "401" not in page


def test_steps_land_on_the_wake_they_measured(client, accounts):
    me = accounts.add("user_me")
    _, device = register(client, me, MAC)
    # 단계도 깨어 있던 시간처럼 다음 보고에 실려 와서 앞 행에 붙고, 표에서는 묶어서 보인다. Wi-Fi 연결(2번)에 드는
    # 공유기 연결은 그 행의 wifi_ms 다.
    db.reports.add_reports([
        {"received_at": "2026-11-12T10:00:00+00:00", "device_id": device, "wifi_ms": 64, "rssi": -55, "wifi_attempts": 1},
        {"received_at": "2026-11-12T10:30:00+00:00", "device_id": device, "wifi_ms": 70, "rssi": -55, "wifi_attempts": 1,
         "prev_awake_ms": 2402, "prev_steps": {"sense": 121, "radio_on": 100, "join": 64, "connect": 987,
                                               "wait": 300, "read": 40, "panel": 90, "refresh": 433}},
    ])
    sign_in(client, me)
    page = client.get("/dashboard").text
    # 깨어 있던, 라디오, 시작·측정, Wi-Fi 연결(100+64), DNS, 암호화 연결, 응답(300+40), 화면(90+433), 나머지, 새 펌웨어
    assert ('<td>2402</td><td class="step"></td><td class="step">121</td><td class="step">164</td>'
            '<td class="step"></td><td class="step">987</td><td class="step">340</td><td class="step">523</td>'
            '<td class="step"></td><td class="step"></td>') in page
    assert 'class="reports"' in page and 'aria-expanded="false"' in page   # 처음에는 측정값 칸이 보인다


def test_step_groups_cover_every_step():
    # 보드가 보내는 단계는 표와 그래프의 묶음 중 꼭 한 곳에 들어간다.
    grouped = [k for _, _, keys in dashboard_reports.STEP_GROUPS for k in keys]
    assert sorted(grouped) == sorted(db.reports.STEP_KEYS)
