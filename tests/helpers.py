"""테스트에서 보드, 사람, Claude 인 척 요청을 보내는 함수."""

import json
import os
import secrets
import time

from workos.session import seal_data

import db
import server

CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def new_token() -> str:
    return secrets.token_hex(32)


def new_code() -> str:
    return "".join(secrets.choice(CODE_CHARS) for _ in range(6))


def sign_in(client, user: str) -> None:
    """대시보드에 로그인한 쿠키를 붙인다."""
    sealed = seal_data({"until": time.time() + 600, "sub": user}, os.environ["WORKOS_COOKIE_PASSWORD"])
    client.cookies.set("emcp_session", sealed)


def post_form(client, user: str, path: str, **form) -> str | None:
    """대시보드 버튼을 누른다. 돌아가는 주소를 돌려준다."""
    sign_in(client, user)
    return client.post(path, data=form, follow_redirects=False).headers.get("location")


def check_in(client, token: str, mac: str, code: str) -> tuple[int, dict]:
    """등록 전 보드가 등록됐는지 묻는다."""
    r = client.post("/weather", json={"mac": mac, "token": token, "claim_code": code})
    return r.status_code, r.json()


def report(client, token: str | None = None, mac: str | None = None, **extra) -> tuple[int, dict]:
    """보드가 상태를 보고한다."""
    body = {"battery_mv": 4000, "wifi_ms": 100, "rssi": -60, **extra}
    if token:
        body["token"] = token
    if mac:
        body["mac"] = mac
    r = client.post("/weather", json=body)
    return r.status_code, r.json()


def mcp(client, user: str, method: str, params: dict | None = None) -> dict:
    """Claude 가 /mcp 를 부른 것처럼. JSON-RPC 결과를 돌려준다."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = client.post("/mcp", json=body, headers={"Authorization": f"Bearer user:{user}",
                                                "Accept": "application/json, text/event-stream"})
    return [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")][0]["result"]


def tool(client, user: str, tool_name: str, /, **args) -> str:
    """Claude 가 도구를 부른다. 결과 문장을 돌려준다. 도구 인자에 name 이 있어서 앞 셋은 위치로만 받는다."""
    return mcp(client, user, "tools/call", {"name": tool_name, "arguments": args})["content"][0]["text"]


def buffered() -> list[dict]:
    """버퍼 파일에 쌓인 보고."""
    with open(server._BUFFER, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def register(client, user: str, mac: str) -> tuple[str, int]:
    """보드가 먼저 묻고, 사람이 대시보드에 코드를 넣는 순서로 등록한다. (토큰, 보드 번호)."""
    token, code = new_token(), new_code()
    check_in(client, token, mac, code)
    assert post_form(client, user, "/dashboard/claim", code=code) == "/dashboard?notice=registered"
    return token, server._by_token[db.hash_token(token)]["id"]
