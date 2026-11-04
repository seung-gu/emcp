"""Claude 가 부르는 MCP 도구."""

from helpers import mcp, register, report, tool

MAC = "TEST00000001"


def test_tools_need_sign_in(client):
    assert client.post("/mcp", json={}).status_code == 401


def test_tool_list(client, accounts):
    names = {t["name"] for t in mcp(client, accounts.add("user_me"), "tools/list")["tools"]}
    assert names == {"register_device", "get_weather_location", "set_weather_location", "rename_device",
                     "search_location"}


def test_owner_changes_location_and_name(client, accounts):
    owner = accounts.add("user_owner")
    token, device = register(client, owner, MAC)
    assert tool(client, owner, "set_weather_location", device_id=device, name="서울",
                latitude=37.57, longitude=126.98) == f"{device}번 보드의 날씨 위치를 서울 (37.57, 126.98) 로 설정했다"
    assert tool(client, owner, "rename_device", device_id=device, name="거실") == f"{device}번 보드 이름을 '거실' 로 바꿨다"
    assert tool(client, owner, "get_weather_location") == f"{device} / 거실 / {MAC} / 서울 (37.57, 126.98) / 주인"
    assert report(client, token, MAC)[1]["city"] == "서울"     # 보드는 다음 보고에서 새 위치의 날씨를 받는다


def test_others_cannot_change_a_board(client, accounts):
    _, device = register(client, accounts.add("user_owner"), MAC)
    other = accounts.add("user_other")
    assert tool(client, other, "get_weather_location") == "연결된 보드가 없다"
    assert tool(client, other, "set_weather_location", device_id=device, name="서울", latitude=37.57,
                longitude=126.98) == f"{device}번 보드의 주인이 아니거나 그런 보드가 없다"
    assert tool(client, other, "rename_device", device_id=device, name="뺏기") == \
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
