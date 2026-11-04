"""펌웨어 자동 업데이트: 릴리스 확인, 보드에 새 버전 알리기, 펌웨어 내려 주기."""

import pytest

import actions
import firmware
from helpers import register, report

MAC = "TEST00000001"
NEW = "c" * 40                 # 릴리스 커밋
OLD = "b" * 40                 # 릴리스에 들어 있는 옛 커밋
XIAO = b"\xe9" + b"x" * 1000   # 펌웨어 파일 대신


@pytest.fixture
def released(releases):
    releases.publish([NEW, OLD, "a" * 40], {"weather-xiao.bin": XIAO})
    firmware.refresh()
    return releases


def test_old_board_gets_the_new_version_and_downloads_it(client, accounts, released):
    token, _ = register(client, accounts.add("user_me"), MAC)
    status, body = report(client, token, MAC, fw_board="weather-xiao", fw_version=OLD[:7])
    assert status == 200 and body["ota_version"] == NEW
    r = client.get(f"/firmware/weather-xiao/{NEW}")
    assert r.status_code == 200 and r.content == XIAO and r.headers["content-length"] == str(len(XIAO))


def test_release_builds_name_the_tag_and_send_the_commit(client, accounts, released):
    """릴리스 빌드는 fw_version 에 태그 이름을, fw_commit 에 커밋을 싣는다. 업데이트는 커밋으로 정한다."""
    token, _ = register(client, accounts.add("user_me"), MAC)
    old = report(client, token, MAC, fw_board="weather-xiao", fw_version="v1", fw_commit=OLD[:7])[1]
    new = report(client, token, MAC, fw_board="weather-xiao", fw_version="v2", fw_commit=NEW[:7])[1]
    assert old["ota_version"] == NEW and "ota_version" not in new


@pytest.mark.parametrize("fields", [
    {"fw_board": "weather-xiao", "fw_version": NEW[:7]},             # 이미 최신
    {"fw_board": "weather-xiao", "fw_version": OLD[:7] + "-dirty"},  # 수정 중인 빌드
    {"fw_board": "weather-xiao", "fw_version": "d" * 7},             # 릴리스에 없는 커밋
    {"fw_board": "weather-xiao", "fw_version": OLD[:7], "battery_mv": 3600},   # 배터리 부족
    {"fw_board": "weather-supermini", "fw_version": OLD[:7]},        # 릴리스에 없는 보드 종류
    {"fw_version": OLD[:7]},                                         # 업데이트를 모르는 옛 펌웨어
    {"fw_board": ["weather-xiao"], "fw_version": OLD[:7]},           # 이상한 값
])
def test_no_offer(client, accounts, released, fields):
    token, _ = register(client, accounts.add("user_me"), MAC)
    status, body = report(client, token, MAC, **fields)
    assert status == 200 and "ota_version" not in body


def test_no_offer_without_a_release(client, accounts):
    actions._firmware_tries = 0
    actions.run_due_jobs()                      # 릴리스가 없으면 404, 받아 둘 것도 없다
    token, _ = register(client, accounts.add("user_me"), MAC)
    assert "ota_version" not in report(client, token, MAC, fw_board="weather-xiao", fw_version=OLD[:7])[1]


def test_only_the_latest_release_is_served(client, released):
    assert client.get(f"/firmware/weather-xiao/{OLD}").status_code == 404
    assert client.get(f"/firmware/weather-supermini/{NEW}").status_code == 404


def test_downloads_are_limited_per_ip(client, released):
    codes = [client.get(f"/firmware/weather-xiao/{NEW}").status_code for _ in range(7)]
    assert codes == [200] * 6 + [429]


def test_keeps_the_last_release_when_github_fails(released):
    released.down = True
    with pytest.raises(Exception):
        firmware.refresh()
    assert firmware.offer("weather-xiao", OLD[:7], 4000) == NEW


def test_fetches_once_after_start_then_only_when_told(client, releases):
    releases.publish([NEW, OLD], {"weather-xiao.bin": XIAO})
    actions._firmware_tries = 0
    actions.run_due_jobs()                          # 켜진 뒤 첫 작업
    assert firmware.current() == NEW
    newer = "e" * 40
    releases.publish([newer, NEW, OLD], {"weather-xiao.bin": b"\xe9new"})
    asked = releases.requests
    for _ in range(3):
        actions.run_due_jobs()
    assert releases.requests == asked and firmware.current() == NEW   # 다시 묻지 않는다
    assert client.post("/firmware/refresh").json() == {"commit": newer}
    assert firmware.binary("weather-xiao", newer) == b"\xe9new"


def test_start_fetch_gives_up_after_ten_tries(releases):
    releases.down = True
    actions._firmware_tries = 0
    for _ in range(12):
        with pytest.raises(Exception):
            actions.run_due_jobs()
        if actions._firmware_tries == actions._FIRMWARE_TRIES:
            break
    actions.run_due_jobs()                          # 열 번 실패한 뒤로는 묻지 않는다
    assert releases.requests == 10


def test_release_workflow_makes_the_server_fetch_now(client, releases):
    assert client.post("/firmware/refresh").json() == {"commit": None}      # 릴리스가 아직 없다
    releases.publish([NEW, OLD], {"weather-xiao.bin": XIAO})
    assert client.post("/firmware/refresh").json() == {"commit": NEW}
    assert firmware.offer("weather-xiao", OLD[:7], 4000) == NEW


def test_refresh_reports_a_failed_fetch(client, released):
    released.down = True
    r = client.post("/firmware/refresh")
    assert r.status_code == 502 and firmware.current() == NEW               # 받아 둔 것은 그대로


def test_refreshes_are_limited(client):
    codes = [client.post("/firmware/refresh").status_code for _ in range(21)]
    assert codes == [200] * 20 + [429]
