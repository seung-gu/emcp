"""보드가 부르는 POST /weather: 등록 확인과 상태 보고."""

import sentry_sdk

from emcp.entry import board_api
from emcp.infra import db
from emcp.service import actions
from emcp.state import devices
from helpers import buffered, check_in, new_code, new_token, post_form, register, report, tool

MAC = "TEST00000001"


def test_person_enters_code_first(client, accounts):
    me = accounts.add("user_me")
    token, code = new_token(), new_code()
    assert post_form(client, me, "/dashboard/claim", code=code.lower()) == "/dashboard?notice=pending"
    status, body = check_in(client, token, MAC, code)
    assert status == 200 and body["registered"] is True and body["city"] == "뮌헨"


def test_board_asks_first_then_claude_enters_code(client, accounts):
    me = accounts.add("user_me")
    token, code = new_token(), new_code()
    assert check_in(client, token, MAC, code) == (200, {"registered": False})
    assert tool(client, me, "register_device", code=code) == actions.ENTER_RESULTS["registered"]
    status, body = check_in(client, token, MAC, code)
    assert status == 200 and body["registered"] is True and body["city"] == "뮌헨"


def test_report_lands_in_buffer(client, accounts):
    token, device = register(client, accounts.add("user_me"), MAC)
    status, body = report(client, token, MAC, fw_version="4.0.0", log=[{"kind": "wifi", "room_c": 21.5}])
    assert status == 200
    assert body["out_temp_c"] == 10 and body["temp_c"] == 10   # 새 이름과, 옛 펌웨어가 읽는 옛 이름
    row = buffered()[-1]
    assert row["device_id"] == device and row["fw_version"] == "4.0.0"
    assert row["log"][0] == {"kind": "wifi", "room_c": 21.5}
    assert row["timezone"] == "Europe/Berlin"
    assert "token" not in row["raw"]


def test_report_with_old_field_name(client, accounts):
    token, _ = register(client, accounts.add("user_me"), MAC)
    report(client, token, MAC, fw="3.9.9")
    assert buffered()[-1]["fw_version"] == "3.9.9"


def test_weather_lookup_fails(client, accounts, open_meteo):
    token, _ = register(client, accounts.add("user_me"), MAC)
    open_meteo.down = True
    assert report(client, token, MAC) == (500, {"city": "뮌헨"})
    assert "out_temp_c" not in buffered()[-1]


def test_release_then_report_is_refused(client, accounts):
    owner, other = accounts.add("user_owner"), accounts.add("user_other")
    token, device = register(client, owner, MAC)
    assert post_form(client, other, "/dashboard/release", device_id=str(device)) == "/dashboard?notice=not_released"
    assert post_form(client, owner, "/dashboard/release", device_id=str(device)) == "/dashboard?notice=released"
    assert report(client, token, MAC) == (401, {"registered": False})


def test_same_account_registers_again_on_same_row(client, accounts):
    me = accounts.add("user_me")
    _, device = register(client, me, MAC)
    _, again = register(client, me, MAC)          # 저장 공간이 지워져 새 토큰과 코드로 다시 등록
    assert again == device


def test_other_account_starts_a_new_row(client, accounts):
    _, first = register(client, accounts.add("user_a"), MAC)
    _, second = register(client, accounts.add("user_b"), MAC)
    assert second != first


def test_code_shown_by_another_board(client):
    code = new_code()
    check_in(client, new_token(), "TEST0000000F", code)
    assert check_in(client, new_token(), "TEST0000000G", code) == (409, {"registered": False, "new_code": True})


def test_code_entered_by_another_account(client, accounts):
    code = new_code()
    post_form(client, accounts.add("user_a"), "/dashboard/claim", code=code)
    assert post_form(client, accounts.add("user_b"), "/dashboard/claim", code=code) == "/dashboard?notice=taken"


def test_unknown_token(client):
    assert report(client, new_token(), MAC) == (401, {"registered": False})


def test_tokenless_report_only_where_allowed(client):
    assert report(client, mac="TESTLEGACY01")[0] == 401
    board_api.ACCEPT_TOKENLESS = True    # emcp-led 설정
    status, body = report(client, mac="TESTLEGACY01")
    assert status == 200 and body["city"] == "뮌헨"
    row = buffered()[-1]
    assert "device_id" not in row and row["mac"] == "TESTLEGACY01"


def test_malformed_check_in(client):
    r = client.post("/weather", json={"mac": MAC, "token": "짧음", "claim_code": new_code()})
    assert r.status_code == 400


def test_malformed_body(client):
    assert client.post("/weather", content=b"not json").status_code == 400
    assert client.post("/weather", json=[1, 2]).status_code == 400


def test_invalid_code_on_dashboard(client, accounts):
    assert post_form(client, accounts.add("user_me"), "/dashboard/claim", code="0O1I!!") == "/dashboard?notice=invalid"


def test_code_entries_are_limited(client, accounts):
    me = accounts.add("user_me")
    for _ in range(actions.CODES_PER_USER):
        post_form(client, me, "/dashboard/claim", code=new_code())
    assert post_form(client, me, "/dashboard/claim", code=new_code()) == "/dashboard?notice=limited"


def test_check_ins_are_limited(client):
    for _ in range(board_api.CHECK_INS_PER_IP):
        check_in(client, new_token(), MAC, new_code())
    assert check_in(client, new_token(), MAC, new_code())[0] == 429


def test_board_registered_elsewhere_is_found_after_reload(client, accounts):
    """다른 앱에서 등록돼 사본에 없는 토큰은 DB 를 다시 읽어 찾는다."""
    token, device = register(client, accounts.add("user_me"), MAC)
    devices._by_token.clear()
    status, _ = report(client, token, MAC)
    assert status == 200 and devices.find(db.boards.hash_token(token))["id"] == device


def test_check_in_failure_reaches_error_alerts(client, monkeypatch):
    """DB 에 닿지 못하면 보드에는 503 으로 답하고(토큰을 지우지 않게), 에러는 Sentry 로 보낸다."""
    sent = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda e=None: sent.append(e))

    def down(*args):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(db.boards, "board_check_in", down)
    assert check_in(client, new_token(), MAC, new_code())[0] == 503
    assert [str(e) for e in sent] == ["database unreachable"]


def test_report_keeps_the_last_wakes_radio_time(client, accounts):
    token, _ = register(client, accounts.add("user_me"), MAC)
    report(client, token, MAC, prev_radio_ms=812)
    assert buffered()[-1]["prev_radio_ms"] == 812


def test_report_keeps_the_last_wakes_steps(client, accounts):
    token, _ = register(client, accounts.add("user_me"), MAC)
    # 모르는 단계, 숫자가 아닌 값, 음수는 버리고 아는 단계의 숫자만 남긴다.
    report(client, token, MAC, prev_steps={"sense": 120, "connect": "987", "tls": 5, "dns": "x", "draw": -3})
    assert buffered()[-1]["prev_steps"] == {"sense": 120, "connect": 987}


def test_report_without_usable_steps_has_none(client, accounts):
    token, _ = register(client, accounts.add("user_me"), MAC)
    report(client, token, MAC, prev_steps=[1, 2, 3])
    assert "prev_steps" not in buffered()[-1]
