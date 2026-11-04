"""emcp: e-ink 날씨 보드 서버. 이 파일은 다른 파일의 경로와 도구를 앱 하나로 묶어 실행한다.

들어오는 곳은 셋이다.
1. MCP (Streamable HTTP, /mcp): tools.py. 로그인한 사람만 쓰고, 도구는 부른 사람의 보드만 다룬다.
2. 보드용 HTTP (POST /weather): board_api.py.
3. 로그인과 대시보드 (/auth, /dashboard, /r): web.py.

그 아래에 입구 여럿이 같이 쓰는 동작(actions.py), 입력 검사(checks.py), 보드 사본(devices.py), 보고
버퍼(buffer.py)가 있고, 맨 아래에 DB(db/), 로그인(auth.py, WorkOS), 날씨와 지명 검색(weather.py,
open-meteo)이 있다. 대시보드 화면은 dashboard/ 가 그린다. 위 층이 아래 층을 부르고, 반대로는 부르지 않는다.

보드 등록 설계는 docs/multi-user-rollout.ko.md 에 있다. 공식 MCP Python SDK(`mcp` 패키지)의 FastMCP 를 쓴다.
"""

import os
import threading

import sentry_sdk
from mcp.server.fastmcp import FastMCP

import actions
import auth
import board_api
import devices
import tools
import web

# 에러 알림. SENTRY_DSN 이 있을 때만 켠다 (fly secrets). 잡지 못한 예외는 Sentry 가 알아서 보내고, 잡아서 로그만
# 남기는 곳(보고 옮기기, 등록 확인, 보드 사본 읽기 등)은 그 자리에서 sentry_sdk.capture_exception 으로 보낸다.
# 요청 본문은 보내지 않는다. 보드 보고에는 토큰이 들어 있다.
if dsn := os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(dsn=dsn, send_default_pii=False, max_request_body_size="never", traces_sample_rate=0)

# Fly.io 는 PORT 환경변수로 내부 포트를 넘겨준다. 없으면 8080.
_port = int(os.environ.get("PORT", "8080"))
# stateless_http: 요청마다 그 요청의 토큰으로 사용자를 정한다. 세션을 이어 쓰면 도구가 세션을 처음 연
# 사람으로 실행될 수 있다 (docs/multi-user-rollout.ko.md 2단계).
mcp = FastMCP("emcp", host="0.0.0.0", port=_port, stateless_http=True,
              **auth.fastmcp_options())

for tool in (tools.register_device, tools.get_weather_location, tools.set_weather_location,
             tools.rename_device, tools.search_location, tools.get_reports):
    mcp.tool()(tool)

ROUTES = [
    ("GET", "/health", board_api.health),
    ("POST", "/weather", board_api.weather_report),
    ("GET", "/auth/login", web.login),
    ("GET", "/auth/callback", web.callback),
    ("GET", "/", web.root),
    ("GET", "/dashboard", web.dashboard_page),
    ("POST", "/dashboard/claim", web.dashboard_claim),
    ("POST", "/dashboard/release", web.dashboard_release),
    ("POST", "/dashboard/history", web.dashboard_history),
    ("POST", "/dashboard/rename", web.dashboard_rename),
    ("GET", "/dashboard/places", web.dashboard_places),
    ("POST", "/dashboard/location", web.dashboard_location),
    ("POST", "/dashboard/share", web.dashboard_share),
    ("POST", "/dashboard/share-answer", web.dashboard_share_answer),
    ("POST", "/dashboard/leave", web.dashboard_leave),
    ("POST", "/dashboard/unshare", web.dashboard_unshare),
    ("GET", "/r/{code}", web.scanned),
]
for method, path, handler in ROUTES:
    mcp.custom_route(path, methods=[method])(handler)


if __name__ == "__main__":
    # 보드 사본을 미리 채운다. 따로 스레드에서 읽으니 DB 가 느려도 서버는 바로 뜬다
    threading.Thread(target=devices.load, daemon=True).start()
    # 한 시간마다 보고를 Neon 으로 옮기는 타이머. 요청이 없어도 돈다
    threading.Thread(target=actions.run_jobs, daemon=True).start()
    mcp.run(transport="streamable-http")
