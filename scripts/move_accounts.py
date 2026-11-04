"""WorkOS 를 Staging 에서 Production 으로 옮긴 뒤, 보드 주인과 공유 기록을 새 사용자 ID 로 넘긴다.

두 환경은 계정이 따로라 같은 사람도 Production 에서 다시 가입하고 새 ID 를 받는다. device_access 에 적어 둔
이메일로 Production 계정을 찾아 짝을 맞춘다. 아직 다시 가입하지 않은 사람은 건너뛰니, 가입한 뒤 다시 돌리면
된다. 기본은 보여 주기만 하고, --apply 를 붙여야 바꾼다.

    uv run --env-file .env python -m scripts.move_accounts            # 무엇이 바뀔지 보기
    uv run --env-file .env python -m scripts.move_accounts --apply    # 바꾸기

.env 의 WORKOS_API_KEY, WORKOS_CLIENT_ID 는 Production 값이어야 한다.
"""

import os
import sys

import auth
import db.boards


def plan() -> list[tuple[str, str, str | None]]:
    """(이메일, 옛 ID, 새 ID). 새 ID 가 None 이면 그 이메일로 Production 에 가입한 계정이 아직 없다. 이미 새 ID
    인 줄은 빼고 돌려준다."""
    out = []
    for row in db.boards.account_emails():
        new_id = auth.user_id_for_email(row["email"])
        if new_id != row["user_id"]:
            out.append((row["email"], row["user_id"], new_id))
    return out


def main(apply: bool) -> None:
    if not os.environ.get("WORKOS_API_KEY", "").startswith("sk_live_"):
        sys.exit("WORKOS_API_KEY 가 Production 키(sk_live_)가 아닙니다. .env 를 Production 값으로 바꾼 뒤 돌리세요.")
    for email, old_id, new_id in plan():
        if new_id is None:
            print(f"{email}: Production 에 아직 가입하지 않음, 건너뜀")
        elif apply:
            print(f"{email}: {old_id} -> {new_id}, {db.boards.move_account(old_id, new_id)}줄 옮김")
        else:
            print(f"{email}: {old_id} -> {new_id} (옮기려면 --apply)")


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])
