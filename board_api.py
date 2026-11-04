"""보드와 Fly 가 부르는 HTTP 경로.

- POST /weather: 등록 전 보드는 토큰과 등록 코드만 보내 등록됐는지 묻는다. 등록된 보드는 깨어날 때마다 토큰을
  실어 상태를 보고하고, 같은 응답으로 그 보드 위치의 날씨를 받는다.
- GET /health: Fly.io 헬스체크.
"""

import os
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

import buffer
import checks
import db.boards
import db.reports
import devices
import weather

# 옛 펌웨어가 DB 와 다른 이름으로 보내는 항목. 4단계 펌웨어는 새 이름으로 보내지만, 옛 펌웨어 보드가 남아
# 있는 동안 둘 다 받는다.
_BOARD_NAMES = {"fw_version": "fw"}
# 보드가 4단계부터 싣는 비밀값. 원본(report_raw)에도 남기지 않는다.
_SECRET_KEYS = ("token", "claim_code")

# 옛 펌웨어 보드는 토큰 없이 MAC 만 보낸다. 세 보드를 다시 플래싱할 때까지, 그 보드들이 보고하는 emcp-led
# 에만 ACCEPT_TOKENLESS=1 을 준다. 옛 펌웨어는 거절당하면 5분마다 다시 시도해서 배터리가 빨리 닳는다.
ACCEPT_TOKENLESS = os.getenv("ACCEPT_TOKENLESS") == "1"

# 요청 횟수 제한 (1시간에). 보드 하나는 등록 전 1시간에 12번 묻는다.
CHECK_INS_PER_IP = 30   # 등록 확인. 한 집에서 두 대를 같이 등록해도 넉넉하게
LOOKUPS_PER_IP = 12     # 사본에 없는 토큰 때문에 DB 를 다시 읽는 횟수


def _field(report: dict, name: str):
    """보고에서 name 항목. 없으면 옛 펌웨어가 쓰던 이름으로 찾는다."""
    v = report.get(name)
    if v is None and name in _BOARD_NAMES:
        v = report.get(_BOARD_NAMES[name])
    return v


async def health(request: Request) -> PlainTextResponse:
    """Fly.io 헬스체크용."""
    return PlainTextResponse("ok")


async def weather_report(request: Request) -> JSONResponse:
    """보드가 부르는 경로. 등록 전 보드의 등록 확인과 등록된 보드의 상태 보고를 받고 날씨를 응답한다.

    보드는 401 과 {"registered": false} 를 받을 때만 토큰을 지우고 새 코드를 띄운다. 그래서 서버가 보드를
    확인하지 못한 경우(DB 장애, 횟수 제한)에는 401 이 아닌 503, 429 로 답한다.
    """
    try:
        report = await request.json()
        if not isinstance(report, dict):
            raise ValueError("not an object")
    except Exception:
        return JSONResponse({"error": "bad body"}, status_code=400)
    ip = request.client.host if request.client else "unknown"
    mac = checks.tag(report.get("mac"))
    if "claim_code" in report:
        return await _check_in(report, mac, ip)

    if "token" in report:
        device = devices.find(token_hash := db.boards.hash_token(str(report["token"])))
        if device is None:
            # 사본에 없으면 DB 를 한 번 다시 읽는다. 모르는 토큰을 쏟아부어 DB 를 계속 깨우지 못하게 IP 마다
            # 횟수를 묶는다.
            if not checks.allowed(f"lookup:{ip}", LOOKUPS_PER_IP):
                return JSONResponse({"error": "too many requests"}, status_code=429)
            if not await run_in_threadpool(devices.load):
                return JSONResponse({"error": "try again later"}, status_code=503)
            device = devices.find(token_hash)
        if device is None:
            return JSONResponse({"registered": False}, status_code=401)
    elif ACCEPT_TOKENLESS:
        device = devices.find_by_mac(mac)
    else:
        return JSONResponse({"registered": False}, status_code=401)

    try:
        battery_mv, wifi_ms, rssi = (int(report[k]) for k in ("battery_mv", "wifi_ms", "rssi"))
    except Exception:
        return JSONResponse({"error": "bad body"}, status_code=400)

    row = {"received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # 토큰으로 온 보고는 보드 번호를 적어 두고, 옛 보고는 Neon 으로 옮길 때 MAC 으로 보드를 찾는다.
    if "token" in report:
        row["device_id"] = device["id"]
    if mac:
        row["mac"] = mac
    # 나머지는 있으면 싣고 없으면 만다. 보드 펌웨어와 이 서버는 따로 배포되므로 한쪽이
    # 아직 모르는 필드가 있어도 보고가 깨지면 안 된다. 항목 목록은 db/reports.py 한 곳에 있다.
    for name, kind in {**db.reports.REPORT_FIELDS, **db.reports.SENSOR_FIELDS}.items():
        v = checks.CLEAN[kind](_field(report, name))
        if v is not None:
            row[name] = v
    log = report.get("log")
    if isinstance(log, list) and log:
        row["log"] = [checks.clean_entry(e) for e in log if isinstance(e, dict)]
    # 받은 그대로도 남긴다. 서버가 아직 모르는 항목을 나중에 칸으로 뽑을 때 옛 보고에서도 채우려고.
    row["raw"] = {k: v for k, v in report.items() if k not in _SECRET_KEYS}

    body, status, record = await weather.board_body(devices.location(device))
    row.update(record)
    buffer.append(row)

    tail = f" +{len(row['log'])} logged" if "log" in row else ""
    if (err := row.get("room_err")) and err != "ok":
        tail += f" sht:{err}(sda{int(bool(row.get('room_sda')))}"
        tail += f" scl{int(bool(row.get('room_scl')))} ack={row.get('room_ack') or '-'})"
    print(f"report: {battery_mv}mV {wifi_ms}ms {rssi}dBm{tail}", flush=True)

    # Neon 으로 옮기는 건 한 시간마다 타이머가 한다 (actions.run_jobs). 보드는 DB 를 기다리지 않는다.
    return JSONResponse(body, status_code=status)


async def _check_in(report: dict, mac: str | None, ip: str) -> JSONResponse:
    """등록 전 보드가 등록됐는지 묻는다. 측정값은 싣지 않으므로 보고로 저장하지 않고, 등록되면 그때부터
    날씨를 준다. 이 경로만 DB 를 바로 쓰는데, 보드 하나가 등록하는 1시간 동안만 묻는다."""
    token, code = checks.token(report.get("token")), checks.code(report.get("claim_code"))
    if not (token and code and mac):
        return JSONResponse({"error": "bad body"}, status_code=400)
    if not checks.allowed(f"check-in:{ip}", CHECK_INS_PER_IP):
        return JSONResponse({"error": "too many requests"}, status_code=429)
    try:
        result, _ = await run_in_threadpool(db.boards.board_check_in, db.boards.hash_token(token), mac, code)
    except Exception as e:
        print(f"check-in failed: {e!r}", flush=True)
        return JSONResponse({"error": "try again later"}, status_code=503)
    if result == "taken":
        # 다른 보드가 같은 코드를 쓰고 있다. 보드는 코드를 새로 만들어 띄운다.
        return JSONResponse({"registered": False, "new_code": True}, status_code=409)
    if result != "registered":
        return JSONResponse({"registered": False})
    await run_in_threadpool(devices.load)
    body, status, _ = await weather.board_body(devices.location(devices.find(db.boards.hash_token(token))))
    print(f"registered: {mac}", flush=True)
    return JSONResponse({**body, "registered": True}, status_code=status)
