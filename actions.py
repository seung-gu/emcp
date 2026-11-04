"""입구 여럿이 같이 쓰는 동작과, 정해진 때마다 도는 작업.

- 대시보드와 Claude 가 같이 쓰는 동작: 등록 코드 입력, 위치 바꾸기, 이름 바꾸기. 결과는 키로 돌려주고, 그걸
  안내 문구나 Claude 답으로 바꾸는 건 부른 쪽(web.py, tools.py)이 한다.
- 보고 옮기기: 버퍼를 Neon 으로 옮기고, 그 김에 지운 계정의 보드를 해제하고, 보드 사본을 다시 읽는다. 이
  순서를 여기서 정해서 buffer.py 가 위 층을 부르지 않게 한다. 한 시간마다 옮기는 건 server.py 가 띄우는
  타이머 스레드(run_jobs)가 하고, 대시보드는 열 때마다 먼저 옮긴다.
- 펌웨어 릴리스 받기: 서버가 켜진 뒤 같은 타이머가 한 번 한다 (firmware.py).
"""

import time
from datetime import datetime, timedelta, timezone

import sentry_sdk
from starlette.concurrency import run_in_threadpool

import auth
import buffer
import checks
import db.boards
import db.core
import devices
import firmware

# --- 등록 코드 입력 ---

# 사람이 코드를 입력하는 횟수 (1시간에). 코드를 찍어서 맞히지 못하게
CODES_PER_USER = 10

# 코드를 입력한 결과. 대시보드 안내와 Claude 답에 같이 쓴다.
ENTER_RESULTS = {
    "registered": "보드를 등록했습니다.",
    "pending": "코드를 받았습니다. 보드의 RESET 버튼을 한 번 누르면 바로 등록됩니다.",
    "taken": "이미 다른 계정이 입력한 코드입니다. 보드 디스플레이의 코드를 다시 확인해 주세요.",
    "invalid": "등록 코드는 보드 디스플레이에 나온 6자리입니다.",
    "limited": "코드를 너무 많이 입력했습니다. 1시간 뒤에 다시 해 주세요.",
    "error": "서버가 잠시 DB에 닿지 못했습니다. 잠시 뒤에 다시 해 주세요.",
}


async def enter_code(user: str, raw) -> str:
    """대시보드와 Claude 가 같이 쓰는 코드 입력. ENTER_RESULTS 의 키를 돌려준다."""
    if not (code := checks.code(raw)):
        return "invalid"
    if not checks.allowed(f"code:{user}", CODES_PER_USER):
        return "limited"
    try:
        email = await run_in_threadpool(auth.user_email, user)
        result, _ = await run_in_threadpool(db.boards.enter_code, user, code, email)
    except Exception as e:
        print(f"entering a code failed: {e!r}", flush=True)
        sentry_sdk.capture_exception(e)
        return "error"
    if result == "registered":
        await run_in_threadpool(devices.load)
    return result


# --- 위치와 이름 바꾸기. 주인만 바꿀 수 있다 ---

async def move_board(user: str, device_id: int, name: str, lat: float, lon: float) -> str:
    """보드의 날씨 위치를 바꾼다. 'moved', 'bad_place'(좌표 범위 밖), 'bad_name'(1~40자가 아님),
    'not_owner'(주인이 아니거나 없는 번호) 중 하나를 돌려준다. 바꾸면 사본을 다시 읽어서 보드가 다음 보고에서
    새 위치의 날씨를 받는다."""
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return "bad_place"
    if not (city := checks.name(name)):
        return "bad_name"
    if not await run_in_threadpool(db.boards.set_location, user, device_id, city, lat, lon):
        return "not_owner"
    await run_in_threadpool(devices.load)
    return "moved"


async def rename_board(user: str, device_id: int, name: str) -> str:
    """보드 이름을 바꾼다. 'renamed', 'bad_name', 'not_owner' 중 하나를 돌려준다."""
    if not (new := checks.name(name)):
        return "bad_name"
    if not await run_in_threadpool(db.boards.rename_device, user, device_id, new):
        return "not_owner"
    return "renamed"


# --- 보고 옮기기 ---

_FLUSH_SECONDS = 3600
_last_flush = float("-inf")     # 뜬 뒤 첫 확인에서, 지난번에 못 옮기고 남은 파일부터 옮긴다
_JOB_SECONDS = 60
_FIRMWARE_TRIES = 10
_firmware_tries = 0


def flush_due() -> bool:
    """마지막으로 옮긴 지 한 시간이 지났으면 버퍼를 봉인하고 True."""
    global _last_flush
    if time.monotonic() - _last_flush < _FLUSH_SECONDS:
        return False
    _last_flush = time.monotonic()
    buffer.seal()
    return True


def run_due_jobs() -> None:
    """할 때가 된 작업을 한다. 한 시간마다 보고 옮기기, 켜진 뒤 한 번 펌웨어 릴리스 받기. 옮길 보고가 없으면
    DB 를 깨우지 않는다. 보드가 모두 꺼져 있을 때 Neon 이 한 시간마다 깨어나지 않게 하려는 것이다."""
    global _firmware_tries
    if flush_due() and buffer.has_sealed():
        flush_reports()
    # 받아 둔 릴리스는 메모리에만 있어서 켜진 뒤 한 번 받는다. 실패하면 1분마다 다시, 10번까지. 그 뒤로는 릴리스
    # 워크플로가 알릴 때만 받는다 (board_api.firmware_refresh).
    if not firmware.loaded() and _firmware_tries < _FIRMWARE_TRIES:
        _firmware_tries += 1
        firmware.refresh()


def run_jobs() -> None:
    """앱이 떠 있는 동안 1분마다 할 일이 있는지 본다. server.py 가 데몬 스레드로 띄운다. 한 번 실패해도
    스레드가 멈추지 않게 예외를 잡는다. 멈추면 보고가 버퍼에 쌓이기만 한다."""
    while True:
        time.sleep(_JOB_SECONDS)
        try:
            run_due_jobs()
        except Exception as e:
            print(f"scheduled job failed: {e!r}", flush=True)
            sentry_sdk.capture_exception(e)


def flush_reports() -> None:
    """봉인된 보고를 Neon 으로 옮기고, 지운 계정의 보드를 해제하고, 보드 사본을 다시 읽는다. 옮기다 실패하면
    거기서 멈추고 다음 번에 다시 한다."""
    if not buffer.flush_sealed():
        return
    forget_deleted_users()
    devices.load()


# WorkOS 에서 지워진 계정의 보드를 해제한다. WorkOS 는 계정이 지워져도 이 서버에 알리지 않으므로, 보고를
# 옮기는 김에(DB 가 어차피 깨어 있을 때) 그 뒤로 지워진 계정을 물어본다. 대시보드를 열 때도 보고를 옮기니
# 매번 WorkOS 를 부르지 않게 30분에 한 번만 묻는다. 어디까지 물어봤는지는 DB 에 둬서 재시작과 두 앱 사이에도
# 이어진다. 처음 켜질 때는 그 전의 삭제를 건너뛰고 그때부터 센다.
_ACCOUNT_CHECK_SECONDS = 1800
_last_account_check = float("-inf")


def forget_deleted_users() -> None:
    global _last_account_check
    if time.monotonic() - _last_account_check < _ACCOUNT_CHECK_SECONDS:
        return
    _last_account_check = time.monotonic()
    try:
        since = db.core.get_state("deleted_users_since")
        if since is None:
            db.core.set_state("deleted_users_since", datetime.now(timezone.utc).isoformat())
            return
        for deleted_at, user_id in auth.deleted_users_since(datetime.fromisoformat(since)):
            released = db.boards.forget_user(user_id)
            print(f"account deleted: {user_id}, released boards {released}", flush=True)
            # 1ms 뒤부터 묻는다. WorkOS 는 시작 시각을 포함해서 돌려주므로 같은 삭제를 또 받지 않게.
            db.core.set_state("deleted_users_since", (deleted_at + timedelta(milliseconds=1)).isoformat())
    except Exception as e:
        print(f"checking deleted accounts failed: {e!r}", flush=True)
        sentry_sdk.capture_exception(e)
