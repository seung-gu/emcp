"""WorkOS Staging -> Production 계정 옮기기 (scripts/move_accounts.py)."""

import db.boards
import db.core
import db.sharing
from helpers import register
from scripts import move_accounts

MAC = "TEST00000001"


def access():
    with db.core.connect() as conn:
        return conn.execute("SELECT device_id, user_id, role, accepted FROM device_access ORDER BY role").fetchall()


def test_owner_and_viewer_move_to_their_new_ids(client, accounts):
    owner = accounts.add("user_staging_owner", "owner@test.example")
    friend = accounts.add("user_staging_friend", "friend@test.example")
    _, device = register(client, owner, MAC)
    db.sharing.share_device(owner, device, friend, "friend@test.example")
    db.sharing.answer_share(friend, device, True)

    # Production 으로 바꾼 뒤: 주인만 다시 가입했다
    accounts.emails.clear()
    accounts.add("user_prod_owner", "owner@test.example")
    assert move_accounts.plan() == [("friend@test.example", "user_staging_friend", None),
                                    ("owner@test.example", "user_staging_owner", "user_prod_owner")]
    assert db.boards.move_account("user_staging_owner", "user_prod_owner") == 1
    assert [(r["user_id"], r["role"]) for r in access()] == [("user_prod_owner", "owner"),
                                                             ("user_staging_friend", "viewer")]
    assert db.boards.list_devices("user_prod_owner")[0]["id"] == device

    # 친구도 가입한 뒤 다시 돌리면 친구 줄만 남아 있다
    accounts.add("user_prod_friend", "friend@test.example")
    assert move_accounts.plan() == [("friend@test.example", "user_staging_friend", "user_prod_friend")]
    db.boards.move_account("user_staging_friend", "user_prod_friend")
    assert move_accounts.plan() == []
    assert db.boards.list_devices("user_prod_friend")[0]["id"] == device


def test_refuses_without_a_production_key(monkeypatch):
    monkeypatch.setenv("WORKOS_API_KEY", "sk_test_not_production")
    try:
        move_accounts.main(apply=True)
    except SystemExit as e:
        assert "sk_live_" in str(e)
    else:
        raise AssertionError("staging key was accepted")
