"""보드 사본. 보고에 DB 를 기다리지 않고 답하려고 devices 를 통째로 메모리에 둔다.

토큰으로 온 보고는 토큰 해시로, 토큰 없는 옛 보고는 MAC 으로 보드를 찾는다. 사본은 서버가 뜰 때, 보고를
Neon 으로 옮길 때 (DB 가 어차피 깨어 있을 때), 위치를 바꾸거나 보드를 등록·해제할 때 다시 읽는다. 그래서
다른 앱(emcp, emcp-led)에서 바꾼 위치는 이 앱이 다음에 보고를 옮길 때 들어온다.
위치를 정하지 않은 보드와 아직 DB 에 없는 보드는 기본 위치로 조회한다.
"""

import sentry_sdk

import db.boards

DEFAULT_LOCATION = {"name": "뮌헨", "lat": 48.14, "lon": 11.58}
_by_token: dict[str, dict] = {}
_by_mac: dict[str | None, dict] = {}


def load() -> bool:
    """DB 에서 보드 사본을 다시 읽는다. 실패하면 있던 사본을 그대로 두고 False 를 돌려준다."""
    global _by_token, _by_mac
    try:
        rows = db.boards.read_devices()
    except Exception as e:
        print(f"loading devices failed, keeping the old copy: {e!r}", flush=True)
        sentry_sdk.capture_exception(e)
        return False
    _by_token = {r["token_hash"]: r for r in rows if r["token_hash"]}
    # 같은 MAC 의 행이 여럿이면 가장 오래된 행. 토큰 없는 옛 보고를 db.reports._device_ids 와 같은 행으로 보낸다.
    _by_mac = {r["mac"]: r for r in sorted(rows, key=lambda r: r["id"], reverse=True)}
    return True


def find(token_hash: str) -> dict | None:
    return _by_token.get(token_hash)


def find_by_mac(mac: str | None) -> dict | None:
    """MAC 을 안 보내는 옛 보드는 None 키로 찾는다 (devices 의 MAC 빈 행)."""
    return _by_mac.get(mac)


def location(device: dict | None) -> dict:
    if device and device["city"]:
        return {"name": device["city"], "lat": device["lat"], "lon": device["lon"]}
    return DEFAULT_LOCATION
