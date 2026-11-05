"""공유: 주인이 이메일로 요청하고, 받는 사람이 수락해야 보인다."""

from datetime import datetime, timezone

import pytest

from emcp.infra import auth, db
from helpers import post_form, register, sign_in

MAC = "TEST00000001"


def setup_board(client, accounts):
    owner = accounts.add("user_owner", "owner@test.example")
    friend = accounts.add("user_friend", "friend@test.example")
    _, device = register(client, owner, MAC)
    post_form(client, owner, "/dashboard/rename", device_id=str(device), name="친구 방")
    with db.core.connect() as conn:
        conn.execute("INSERT INTO reports (device_id, received_at, battery_mv, rssi, timezone)"
                     " VALUES (%s, %s, 3999, -60, 'Asia/Seoul')",
                     (device, datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)))
    return owner, friend, device


def share(client, owner, device, email):
    return post_form(client, owner, "/dashboard/share", device_id=str(device), email=email)


def test_share_input_checks(client, accounts):
    owner, _, device = setup_board(client, accounts)
    assert share(client, owner, device, "nobody@test.example") == "/dashboard?notice=email_not_found"
    assert share(client, owner, device, "이메일아님") == "/dashboard?notice=email_not_found"
    assert share(client, owner, device, "owner@test.example") == "/dashboard?notice=self_share"
    assert share(client, owner, device, " Friend@Test.example ") == "/dashboard?notice=shared"
    page = client.get("/dashboard").text
    assert "friend@test.example" in page and "<summary>공유 1</summary>" in page and "수락 대기" in page


def test_nothing_shows_before_accepting(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    sign_in(client, friend)
    page = client.get("/dashboard").text
    assert "owner@test.example 님이 '친구 방' 보드를 공유하려고 합니다" in page
    assert "2026-11-03 21:00" not in page and "목록에서 빼기" not in page
    assert db.boards.list_devices(friend) == []


def test_decline_removes_the_request(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    assert post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="decline") == \
        "/dashboard?notice=share_declined"
    sign_in(client, owner)
    assert "friend@test.example" not in client.get("/dashboard").text


def test_released_board_leaves_every_dashboard(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept")
    assert post_form(client, owner, "/dashboard/release", device_id=str(device)) == "/dashboard?notice=released"
    assert db.boards.list_devices(owner) == [] and db.boards.list_devices(friend) == []
    _, again = register(client, owner, MAC)       # 같은 계정이 다시 등록하면 새 행에서 예전 기록을 묻는다
    assert again != device
    assert [h["device_id"] for h in db.boards.previous_history(owner)] == [again]


def test_accepted_board_is_read_only(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    assert post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept") == \
        "/dashboard?notice=share_accepted"
    page = client.get("/dashboard").text
    assert "친구 방" in page and "<h2>공유받은 보드</h2>" in page and "2026-11-03 21:00" in page   # 서울 시각
    for button in ("<summary>이름 바꾸기</summary>", "<summary>위치 바꾸기</summary>", "<summary>공유", ">등록 해제<"):
        assert button not in page
    assert "목록에서 빼기" in page
    assert share(client, friend, device, "owner@test.example") == "/dashboard?notice=not_shared"
    assert post_form(client, friend, "/dashboard/rename", device_id=str(device), name="뺏기") == \
        "/dashboard?notice=not_renamed"
    sign_in(client, owner)
    assert "공유 중" in client.get("/dashboard").text


def test_accepting_shares_the_boards_i_pick(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    _, picked = register(client, friend, "TEST00000002")
    _, kept = register(client, friend, "TEST00000003")
    share(client, owner, device, "friend@test.example")
    sign_in(client, friend)
    page = client.get("/dashboard").text
    for board in (picked, kept):                   # 처음엔 내 보드가 전부 골라져 있다
        assert f'name="share_back" value="{board}" checked' in page
    post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept",
              share_back=[str(picked), str(device)])   # 남의 보드 번호는 건너뛴다
    register(client, friend, "TEST00000004")       # 수락한 뒤에 등록한 보드는 따로 공유해야 보인다
    assert [(b["id"], b["role"]) for b in db.boards.list_devices(owner)] == [(device, "owner"), (picked, "viewer")]
    assert "공유 중" in client.get("/dashboard").text


@pytest.mark.parametrize("who", ["owner", "friend"])
def test_either_side_cuts_both_ways(client, accounts, who):
    owner, friend, device = setup_board(client, accounts)
    _, friends_board = register(client, friend, "TEST00000002")
    share(client, owner, device, "friend@test.example")
    post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept",
              share_back=[str(friends_board)])
    if who == "owner":
        post_form(client, owner, "/dashboard/unshare", device_id=str(device), email="friend@test.example")
    else:
        post_form(client, friend, "/dashboard/leave", device_id=str(device))
    assert [b["role"] for b in db.boards.list_devices(owner)] == ["owner"]
    assert [b["role"] for b in db.boards.list_devices(friend)] == ["owner"]


def test_shared_board_shows_its_name_not_its_mac(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    sign_in(client, friend)
    assert "'친구 방'" in client.get("/dashboard").text and MAC not in client.get("/dashboard").text
    post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept")
    page = client.get("/dashboard").text
    assert "친구 방" in page and f"/dashboard?board={device}" in page and MAC not in page
    sign_in(client, owner)
    assert MAC in client.get("/dashboard").text


def test_friend_leaves(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept")
    assert post_form(client, friend, "/dashboard/leave", device_id=str(device)) == "/dashboard?notice=left"
    assert "친구 방" not in client.get("/dashboard").text
    sign_in(client, owner)
    assert "friend@test.example" not in client.get("/dashboard").text


def test_owner_unshares(client, accounts):
    owner, friend, device = setup_board(client, accounts)
    share(client, owner, device, "friend@test.example")
    post_form(client, friend, "/dashboard/share-answer", device_id=str(device), choice="accept")
    assert post_form(client, owner, "/dashboard/unshare", device_id=str(device), email="friend@test.example") == \
        "/dashboard?notice=unshared"
    sign_in(client, friend)
    assert "친구 방" not in client.get("/dashboard").text


def test_stranger_sees_nothing(client, accounts):
    setup_board(client, accounts)
    sign_in(client, accounts.add("user_stranger"))
    assert "친구 방" not in client.get("/dashboard").text


def test_workos_down_while_sharing(client, accounts, monkeypatch):
    owner, _, device = setup_board(client, accounts)

    def down(email):
        raise RuntimeError("WorkOS unreachable")

    monkeypatch.setattr(auth, "user_id_for_email", down)
    assert share(client, owner, device, "friend@test.example") == "/dashboard?notice=share_error"
