"""사람이 브라우저로 쓰는 경로: 로그인, 대시보드와 그 버튼들, 보드 QR 이 여는 주소.

대시보드는 로그인한 사람이 볼 수 있는 보드의 보고를 그리고, 보드를 등록하거나 해제하고, 이름과 날씨 위치를
바꾸고, 다른 사람과 공유한다. 버튼은 모두 POST 를 받고 ?notice= 를 단 대시보드로 돌려보낸다.
"""

import time

import sentry_sdk
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from emcp.entry import dashboard
from emcp.infra import auth, db, weather
from emcp.service import actions, checks
from emcp.state import buffer, devices

# 로그인 쿠키. HttpOnly: 페이지 스크립트가 못 읽는다. Secure: https 에서만 (로컬 http 는 예외).
# SameSite=Lax: 다른 사이트에서 보낸 POST 에는 실리지 않는다.
_COOKIE = {"httponly": True, "secure": auth.PUBLIC_URL.startswith("https://"), "samesite": "lax"}


async def login(request: Request) -> RedirectResponse:
    """WorkOS 로그인 화면으로 보낸다. 로그인이 끝나면 WorkOS 가 브라우저를 /auth/callback 으로 돌려보낸다."""
    url, started = auth.start_sign_in()
    response = RedirectResponse(url)
    # state 와 PKCE 검증값은 브라우저가 돌아올 때까지 봉인한 쿠키에 맡겨 둔다
    response.set_cookie("emcp_login", started, max_age=600, **_COOKIE)
    return response


async def callback(request: Request):
    try:
        session = await run_in_threadpool(
            auth.finish_sign_in,
            request.cookies.get("emcp_login", ""),
            request.query_params.get("code"),
            request.query_params.get("state"),
        )
    except auth.SignInError as e:
        return HTMLResponse(str(e), status_code=403)
    response = RedirectResponse("/dashboard")
    response.set_cookie("emcp_session", session, max_age=auth.SIGN_IN_HOURS * 3600, **_COOKIE)
    response.delete_cookie("emcp_login")
    return response


async def root(request: Request) -> RedirectResponse:
    return RedirectResponse("/dashboard")


# 대시보드 위쪽에 띄우는 안내. 주소의 ?notice= 는 이 키만 받으므로 다른 글자가 페이지에 들어가지 않는다.
NOTICES = {
    **actions.ENTER_RESULTS,
    "released": "등록을 해제했습니다. 보드는 다음 보고 때 새 코드를 띄웁니다. 지난 기록은 계속 볼 수 있습니다.",
    "not_released": "내가 주인인 등록된 보드만 등록을 해제할 수 있습니다.",
    "prefilled": "보드 디스플레이의 코드를 채워 두었습니다. '보드 등록'을 누르세요.",
    "imported": "예전 기록을 이 보드로 가져왔습니다.",
    "kept": "예전 기록은 가져오지 않고 그대로 두었습니다. 다시 묻지 않습니다.",
    "not_imported": "가져올 예전 기록이 없거나 내가 주인인 보드가 아닙니다.",
    "renamed": "보드 이름을 바꿨습니다.",
    "not_renamed": "이름은 1~40자여야 하고, 내가 주인인 보드만 바꿀 수 있습니다.",
    "located": "날씨 위치를 바꿨습니다. 보드가 다음에 깨어날 때(30분 안) 화면에 반영됩니다.",
    "not_located": "위치를 바꾸지 못했습니다. 검색한 후보 중 하나를 골라 주세요.",
    "shared": "공유를 요청했습니다. 상대가 대시보드에서 수락하면 이 보드가 보입니다.",
    "share_accepted": "공유를 수락했습니다. 이제 이 보드가 보입니다.",
    "share_declined": "공유 요청을 거절했습니다.",
    "left": "공유받던 보드를 목록에서 뺐습니다.",
    "not_answered": "이미 처리된 요청입니다.",
    # 공유 버튼은 내 보드에만 있어서, 이건 페이지를 띄워 둔 사이에 보드가 바뀐 경우(등록 해제, 예전 기록 합치기 등)다.
    "not_shared": "공유하지 못했습니다. 페이지를 새로고침한 뒤 다시 해 주세요.",
    "self_share": "내 이메일로는 공유할 수 없습니다.",
    "email_not_found": "이메일 주소를 찾을 수 없습니다.",
    "share_error": "지금은 공유할 수 없습니다. 잠시 뒤에 다시 해 주세요.",
    "unshared": "공유를 취소했습니다.",
}


# 대시보드 로그인은 이 서버가 만든 쿠키라 WorkOS 에서 계정을 지워도 12시간 살아 있다. 그래서 대시보드에
# 들어올 때마다 계정이 아직 있는지 WorkOS 에 묻는다. 같은 사람은 10분에 한 번만 묻고, 지워진 계정은 계속
# 지워진 것으로 기억한다. 보드를 바꾸는 버튼(코드 입력, 등록 해제, 이름·위치 바꾸기)도 모두 이걸 거친다.
_ALIVE_SECONDS = 600
_alive: dict[str, tuple[float, bool]] = {}


async def _signed_in(request: Request) -> str | None:
    """로그인한 사람의 사용자 ID. 쿠키가 없거나, 만료됐거나, 그 계정이 WorkOS 에서 지워졌으면 None."""
    user = auth.session_user(request.cookies.get("emcp_session", ""))
    if not user:
        return None
    checked_at, alive = _alive.get(user, (float("-inf"), True))
    if alive and time.monotonic() - checked_at >= _ALIVE_SECONDS:
        exists = await run_in_threadpool(auth.user_exists, user)
        if exists is not None:              # 모르면 지금 상태를 유지하고 다음에 다시 묻는다
            alive = exists
            _alive[user] = (time.monotonic(), alive)
    return user if alive else None


async def dashboard_page(request: Request):
    """로그인한 사람이 볼 수 있는 보드의 보고를 훑어보고, 보드를 등록하거나 등록을 해제하는 페이지.

    ?mac=... 을 주면 그 보드만. 안 주면 전부 섞어서 보여주고 표에 보드 열이 붙는다. 남의 MAC 을 넣으면
    보고가 없는 빈 화면이 나온다. 그 보드가 있는지조차 알려 주지 않는다.
    아직 Neon 으로 옮기지 않은 보고를 먼저 옮긴 뒤 읽는다. 그래야 방금 들어온 보고까지 보인다.
    """
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login")
    buffer.seal()
    await run_in_threadpool(actions.flush_reports)
    rows = await run_in_threadpool(db.reports.read_reports, user)
    boards = await run_in_threadpool(db.boards.list_devices, user)
    pending = await run_in_threadpool(db.boards.pending_codes, user)
    history = await run_in_threadpool(db.boards.previous_history, user)
    shares = await run_in_threadpool(db.sharing.shares_of, user)
    incoming = await run_in_threadpool(db.sharing.incoming_shares, user)
    # QR 로 왔으면 코드를 입력 칸에 채운다. 로그인을 거쳐 왔으면 /r 이 쿠키에 맡겨 둔 코드다.
    prefill = checks.code(request.query_params.get("code")) or checks.code(request.cookies.get("emcp_claim"))
    notice = NOTICES.get(request.query_params.get("notice", "")) or (prefill and NOTICES["prefilled"])
    response = HTMLResponse(dashboard.page.render(rows, checks.tag(request.query_params.get("mac")), boards=boards,
                                             pending=pending, notice=notice, prefill=prefill, history=history,
                                             shares=shares, incoming=incoming))
    if "emcp_claim" in request.cookies:
        response.delete_cookie("emcp_claim")
    return response


async def dashboard_claim(request: Request) -> RedirectResponse:
    """대시보드에서 보드 디스플레이의 등록 코드를 입력한다. 로그인 쿠키는 SameSite=Lax 라 다른 사이트가
    보낸 POST 에는 실리지 않는다."""
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    result = await actions.enter_code(user, form.get("code"))
    return RedirectResponse(f"/dashboard?notice={result}", status_code=303)


async def dashboard_release(request: Request) -> RedirectResponse:
    """보드의 등록을 해제한다. 보드를 남에게 넘길 때 전 주인이 누른다."""
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = checks.to_int(form.get("device_id"))
    released = device_id is not None and await run_in_threadpool(db.boards.release_device, user, device_id)
    if released:
        await run_in_threadpool(devices.load)
    return RedirectResponse(f"/dashboard?notice={'released' if released else 'not_released'}", status_code=303)


async def dashboard_history(request: Request) -> RedirectResponse:
    """등록된 보드에 예전 기록이 있을 때 "가져오기"(choice=import) 또는 "그냥 두기"(choice=keep). 예전 기록은
    주인 이메일이 같은 경우에만 대시보드에 뜬다 (db.boards.previous_history)."""
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = checks.to_int(form.get("device_id"))
    if device_id is None:
        return RedirectResponse("/dashboard?notice=not_imported", status_code=303)
    if form.get("choice") == "import":
        done = await run_in_threadpool(db.boards.import_history, user, device_id)
        notice = "imported" if done else "not_imported"
    else:
        done = await run_in_threadpool(db.boards.decline_history, user, device_id)
        notice = "kept" if done else "not_imported"
    return RedirectResponse(f"/dashboard?notice={notice}", status_code=303)


# 대시보드에서 이름과 날씨 위치 바꾸기. Claude 의 rename_device, set_weather_location 과 같은 actions 함수를
# 부르므로 어느 쪽에서 바꿔도 같은 값이다. 주인만 바꿀 수 있다.
async def dashboard_rename(request: Request) -> RedirectResponse:
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = checks.to_int(form.get("device_id"))
    done = (device_id is not None
            and await actions.rename_board(user, device_id, str(form.get("name") or "")) == "renamed")
    return RedirectResponse(f"/dashboard?notice={'renamed' if done else 'not_renamed'}", status_code=303)


async def dashboard_places(request: Request) -> JSONResponse:
    """위치 바꾸기의 지명 검색. 결과는 브라우저가 textContent 로 그리므로 지명에 무엇이 들어 있어도 된다."""
    if not await _signed_in(request):
        return JSONResponse({"error": "sign in"}, status_code=401)
    query = (request.query_params.get("q") or "").strip()[:60]
    if not query:
        return JSONResponse([])
    results = await weather.geocode(query, 6)
    if results is None:
        return JSONResponse({"error": "search failed"}, status_code=502)
    return JSONResponse([
        {"name": r["name"], "where": " · ".join(x for x in (r.get("admin1"), r.get("country")) if x),
         "lat": r["latitude"], "lon": r["longitude"]}
        for r in results
    ])


async def dashboard_location(request: Request) -> RedirectResponse:
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = checks.to_int(form.get("device_id"))
    try:
        lat, lon = float(form.get("lat")), float(form.get("lon"))
    except (TypeError, ValueError):
        lat = lon = float("nan")             # actions.move_board 의 범위 검사에서 걸린다
    done = (device_id is not None
            and await actions.move_board(user, device_id, str(form.get("name") or ""), lat, lon) == "moved")
    return RedirectResponse(f"/dashboard?notice={'located' if done else 'not_located'}", status_code=303)


# 공유: 주인이 친구 이메일로 공유를 요청한다. 그 이메일의 계정은 WorkOS 에 한 번 물어서 찾는다.
async def dashboard_share(request: Request) -> RedirectResponse:
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id, email = checks.to_int(form.get("device_id")), checks.email(form.get("email"))
    if device_id is None or not email:
        return RedirectResponse("/dashboard?notice=email_not_found", status_code=303)
    try:
        viewer = await run_in_threadpool(auth.user_id_for_email, email)
    except Exception as e:
        print(f"looking up {email} failed: {e!r}", flush=True)
        sentry_sdk.capture_exception(e)
        return RedirectResponse("/dashboard?notice=share_error", status_code=303)
    if not viewer:
        return RedirectResponse("/dashboard?notice=email_not_found", status_code=303)
    if viewer == user:
        return RedirectResponse("/dashboard?notice=self_share", status_code=303)
    done = await run_in_threadpool(db.sharing.share_device, user, device_id, viewer, email)
    return RedirectResponse(f"/dashboard?notice={'shared' if done else 'not_shared'}", status_code=303)


async def dashboard_share_answer(request: Request) -> RedirectResponse:
    """받은 공유 요청에 수락(choice=accept) 또는 거절."""
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id, accept = checks.to_int(form.get("device_id")), form.get("choice") == "accept"
    done = device_id is not None and await run_in_threadpool(db.sharing.answer_share, user, device_id, accept)
    notice = ("share_accepted" if accept else "share_declined") if done else "not_answered"
    return RedirectResponse(f"/dashboard?notice={notice}", status_code=303)


async def dashboard_leave(request: Request) -> RedirectResponse:
    """공유받던 보드를 내 목록에서 뺀다."""
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id = checks.to_int(form.get("device_id"))
    done = device_id is not None and await run_in_threadpool(db.sharing.leave_share, user, device_id)
    return RedirectResponse(f"/dashboard?notice={'left' if done else 'not_answered'}", status_code=303)


async def dashboard_unshare(request: Request) -> RedirectResponse:
    user = await _signed_in(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    form = await request.form()
    device_id, email = checks.to_int(form.get("device_id")), checks.email(form.get("email"))
    done = device_id is not None and email and await run_in_threadpool(db.sharing.unshare_device, user, device_id, email)
    return RedirectResponse(f"/dashboard?notice={'unshared' if done else 'not_shared'}", status_code=303)


async def scanned(request: Request) -> RedirectResponse:
    """보드 디스플레이의 QR 이 여는 주소. 코드가 채워진 대시보드로 보낸다. 로그인이 안 돼 있으면 로그인을
    거친 뒤에도 코드가 남도록 쿠키에 10분 맡겨 둔다.

    여는 것만으로 등록하지는 않는다. 남이 보낸 링크를 눌렀다고 그 사람의 보드가 내 계정에 붙으면 안 되니,
    대시보드에서 '보드 등록' 을 눌러야 등록된다."""
    code = checks.code(request.path_params.get("code"))
    if not code:
        return RedirectResponse("/dashboard")
    if await _signed_in(request):
        return RedirectResponse(f"/dashboard?code={code}")
    response = RedirectResponse("/auth/login")
    response.set_cookie("emcp_claim", code, max_age=600, **_COOKIE)
    return response
