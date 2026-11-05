"""Claude 가 부르는 MCP 도구."""

from datetime import datetime, timedelta, timezone

from emcp.infra import db
from helpers import mcp, register, report, tool

MAC = "TEST00000001"


def test_tools_need_sign_in(client):
    assert client.post("/mcp", json={}).status_code == 401


def test_tool_list(client, accounts):
    names = {t["name"] for t in mcp(client, accounts.add("user_me"), "tools/list")["tools"]}
    assert names == {"register_device", "get_weather_location", "set_weather_location", "rename_device",
                     "set_auto_update", "search_location", "get_reports"}


def test_owner_changes_location_and_name(client, accounts):
    owner = accounts.add("user_owner")
    token, device = register(client, owner, MAC)
    assert tool(client, owner, "set_weather_location", device_id=device, name="서울",
                latitude=37.57, longitude=126.98) == f"{device}번 보드의 날씨 위치를 서울 (37.57, 126.98) 로 설정했다"
    assert tool(client, owner, "rename_device", device_id=device, name="거실") == f"{device}번 보드 이름을 '거실' 로 바꿨다"
    assert tool(client, owner, "get_weather_location") == \
        f"{device} / 거실 / {MAC} / 서울 (37.57, 126.98) / 주인 / 보고 없음 (최신 확인 중, 자동 업데이트 켜짐)"
    assert report(client, token, MAC)[1]["city"] == "서울"     # 보드는 다음 보고에서 새 위치의 날씨를 받는다


def test_others_cannot_change_a_board(client, accounts):
    _, device = register(client, accounts.add("user_owner"), MAC)
    other = accounts.add("user_other")
    assert tool(client, other, "get_weather_location") == "연결된 보드가 없다"
    assert tool(client, other, "set_weather_location", device_id=device, name="서울", latitude=37.57,
                longitude=126.98) == f"{device}번 보드의 주인이 아니거나 그런 보드가 없다"
    assert tool(client, other, "rename_device", device_id=device, name="뺏기") == \
        f"{device}번 보드의 주인이 아니거나 그런 보드가 없다"
    assert tool(client, other, "set_auto_update", device_id=device, on=False) == \
        f"{device}번 보드의 주인이 아니거나 그런 보드가 없다"


def test_bad_location_input(client, accounts):
    owner = accounts.add("user_owner")
    _, device = register(client, owner, MAC)
    assert tool(client, owner, "set_weather_location", device_id=device, name="  ", latitude=37.57,
                longitude=126.98) == "도시 이름은 1~40자여야 한다"
    assert tool(client, owner, "set_weather_location", device_id=device, name="서울", latitude=99,
                longitude=0).startswith("좌표 범위가 잘못됐다")
    assert tool(client, owner, "rename_device", device_id=device, name="") == "보드 이름은 1~40자여야 한다"


def test_search_location(client, accounts):
    found = tool(client, accounts.add("user_me"), "search_location", query="Berlin")
    assert found.splitlines()[0] == "베를린 / 베를린 / 독일 / 52.52,13.41 / 인구 3426354"


def test_get_reports_reads_recent_reports(client, accounts):
    owner = accounts.add("user_owner")
    token, device = register(client, owner, MAC)
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    db.reports.add_reports([{"received_at": an_hour_ago, "device_id": device, "battery_mv": 4100, "rssi": -55,
                             "wifi_ms": 90, "reset_reason": 8, "room_c": 21.4, "room_rh": 45.0,
                             "fw_version": "6b81cf0", "timezone": "Europe/Berlin"}])
    report(client, token, MAC, reset_reason=1, room_c=21.6, room_rh=44.0, fw_version="6b81cf0",
           log=[{"kind": "wifi", "wifi_ms": 3000}])                  # 버퍼에만 있는 보고
    lines = tool(client, owner, "get_reports", device_id=device).splitlines()
    assert lines[0] == f"{MAC} ({MAC}) / 최근 24시간 보고 2건 / 시각은 Europe/Berlin"
    assert "배터리 4000 mV (기간 중 최저 4000, 최고 4100)" in lines[1] and "실내 21.6°C 44.0%" in lines[1]
    assert lines[2] == "평소와 다른 깨어남 1건 (전원투입), 못 보낸 보고 1건"
    assert lines[5].endswith("/ 4000 / -60 / 100 / 21.6 / 44.0 / 전원투입")   # 가장 최근 보고가 위
    assert lines[6].endswith("/ 4100 / -55 / 90 / 21.4 / 45.0 / ")


def test_get_reports_only_for_boards_i_can_see(client, accounts):
    owner, friend = accounts.add("user_owner"), accounts.add("user_friend")
    token, device = register(client, owner, MAC)
    report(client, token, MAC)
    assert tool(client, friend, "get_reports", device_id=device).startswith(f"{device}번 보드는 볼 수 없다")
    db.sharing.share_device(owner, device, friend, "user_friend@test.example")
    assert tool(client, friend, "get_reports", device_id=device).startswith(f"{device}번 보드는 볼 수 없다")  # 수락 전
    db.sharing.answer_share(friend, device, True)
    assert "보고 1건" in tool(client, friend, "get_reports", device_id=device, hours=0)   # 0 은 1시간으로


def test_get_reports_without_reports(client, accounts):
    owner = accounts.add("user_owner")
    _, device = register(client, owner, MAC)
    assert tool(client, owner, "get_reports", device_id=device) == f"{MAC}: 최근 24시간 동안 보고가 없다"


def test_owner_turns_auto_update_off_and_on(client, accounts):
    owner = accounts.add("user_owner")
    _, device = register(client, owner, MAC)
    assert tool(client, owner, "set_auto_update", device_id=device, on=False) == \
        f"{device}번 보드의 펌웨어 자동 업데이트를 껐다"
    assert tool(client, owner, "get_weather_location").endswith("자동 업데이트 꺼짐)")
    assert tool(client, owner, "set_auto_update", device_id=device, on=True) == \
        f"{device}번 보드의 펌웨어 자동 업데이트를 켰다"
