"""버퍼에 모은 보고를 Neon 으로 옮기기, 그 김에 지운 계정의 보드를 해제하기."""

import glob

import actions
import buffer
import db.boards
import db.reports
import devices
from helpers import buffered, register, report, sign_in

MAC = "TEST00000001"


def test_report_only_writes_to_the_buffer(client, accounts):
    """한 시간이 지났어도 보고 요청은 버퍼에 쓰기만 한다. 옮기는 건 타이머다."""
    me = accounts.add("user_me")
    token, _ = register(client, me, MAC)
    actions._last_flush = float("-inf")
    report(client, token, MAC)
    assert len(buffered()) == 1 and db.reports.read_reports(me) == []


def test_timer_moves_reports_without_any_request(client, accounts):
    me = accounts.add("user_me")
    token, _ = register(client, me, MAC)
    report(client, token, MAC)
    actions._last_flush = float("-inf")
    actions.run_due_jobs()
    assert len(db.reports.read_reports(me)) == 1


def test_timer_leaves_the_database_alone_without_reports(monkeypatch):
    loads = []
    monkeypatch.setattr(devices, "load", lambda: loads.append(1))
    actions._last_flush = float("-inf")
    actions.run_due_jobs()
    assert loads == []


def test_flush_moves_reports_into_the_database(client, accounts):
    me = accounts.add("user_me")
    token, _ = register(client, me, MAC)
    report(client, token, MAC, log=[{"kind": "wifi", "wifi_ms": 3000}])
    buffer.seal()
    actions.flush_reports()
    rows = db.reports.read_reports(me)
    assert len(rows) == 1 and rows[0]["city"] == "뮌헨" and rows[0]["log"][0]["kind"] == "wifi"
    assert glob.glob(buffer.PATH + "*") == []


def test_same_batch_twice_is_stored_once(client, accounts):
    me = accounts.add("user_me")
    token, device = register(client, me, MAC)
    report(client, token, MAC)
    row = {"received_at": "2026-11-04T10:00:00+00:00", "device_id": device, "battery_mv": 4000}
    assert db.reports.add_reports([row])["reports"] == 1
    assert db.reports.add_reports([row]) == {"reports": 0, "skipped": 1, "wake_logs": 0}


def test_dashboard_moves_reports_before_reading(client, accounts):
    me = accounts.add("user_me")
    token, _ = register(client, me, MAC)
    report(client, token, MAC)
    sign_in(client, me)
    client.get("/dashboard")
    assert len(db.reports.read_reports(me)) == 1


def test_flush_is_due_once_an_hour(client, accounts):
    token, _ = register(client, accounts.add("user_me"), MAC)
    assert actions.flush_due() is False  # fresh_state 가 방금 옮긴 것으로 해 둠
    actions._last_flush = float("-inf")
    assert actions.flush_due() is True


def test_deleted_account_loses_its_boards(client, accounts):
    owner = accounts.add("user_owner")
    token, _ = register(client, owner, MAC)
    actions.flush_reports()  # 처음 확인: 지금부터 세기 시작
    accounts.delete(owner)
    actions._last_account_check = float("-inf")  # 30분이 지난 것으로
    actions.flush_reports()
    assert report(client, token, MAC) == (401, {"registered": False})
    assert db.boards.list_devices(owner) == []
