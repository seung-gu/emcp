"""테스트 공통 준비. 운영 DB 와 외부 서비스에는 닿지 않는다.

- DB: Docker 로 띄운 로컬 Postgres (scripts/test-db.sh). 주소가 로컬이 아니면 테스트를 시작하지 않는다.
  테스트마다 테이블을 비운다.
- WorkOS: auth 의 바깥 호출을 가짜 계정 목록(Accounts)으로 바꾼다. Claude 토큰은 "user:<사용자 ID>".
- open-meteo: httpx 요청에 정해 둔 응답을 돌려준다. 날씨와 지명 검색 코드 자체는 그대로 돈다.
- 보고 버퍼: 임시 폴더.
"""

import glob
import os
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg
import pytest
from cryptography.fernet import Fernet

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:54329/emcp_test")
if urlparse(TEST_DB).hostname not in ("localhost", "127.0.0.1", "postgres"):   # postgres: CI 의 서비스 이름
    pytest.exit(f"테스트 DB 는 로컬이어야 합니다: {TEST_DB}", returncode=2)

# 앱 모듈이 읽기 전에 정한다. 진짜 키가 셸에 있어도 덮어써서, 가짜를 빠뜨린 호출은 실패하게 한다.
os.environ.update({
    "DATABASE_URL": TEST_DB,
    "REPORTS_BUFFER": os.path.join(tempfile.mkdtemp(prefix="emcp-test-"), "pending-reports.jsonl"),
    "MCP_AUTH_ISSUER": "https://auth.test.example",
    "PUBLIC_URL": "http://localhost:8080",
    "WORKOS_API_KEY": "sk_test_not_a_key",
    "WORKOS_CLIENT_ID": "client_test",
    "WORKOS_COOKIE_PASSWORD": Fernet.generate_key().decode(),
})
os.environ.pop("ACCEPT_TOKENLESS", None)

for _ in range(60):   # 막 띄운 컨테이너는 몇 초 뒤에 연결을 받는다
    try:
        psycopg.connect(TEST_DB).close()
        break
    except psycopg.OperationalError:
        time.sleep(0.5)
else:
    pytest.exit("테스트 DB 에 닿지 못했습니다. sh scripts/test-db.sh 로 띄우세요.", returncode=2)

import httpx  # noqa: E402
from mcp.server.auth.provider import AccessToken  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


# --- open-meteo ---

FORECAST = {
    "current": {"temperature_2m": 10.4, "weather_code": 0, "wind_speed_10m": 5.2, "relative_humidity_2m": 50},
    "daily": {"temperature_2m_max": [12.6], "temperature_2m_min": [3.1], "precipitation_probability_max": [10]},
    "utc_offset_seconds": 3600,
    "timezone": "Europe/Berlin",
}
PLACES = {"results": [
    {"name": "베를린", "latitude": 52.52, "longitude": 13.41, "admin1": "베를린", "country": "독일", "population": 3426354},
    {"name": "벌린", "latitude": 44.47, "longitude": -71.19, "admin1": "뉴햄프셔주", "country": "미국", "population": 9367},
]}


class OpenMeteo:
    """open-meteo 대신 답한다. down 이면 500."""

    def __init__(self):
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(500)
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json=FORECAST)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(200, json=PLACES)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def open_meteo(monkeypatch):
    fake = OpenMeteo()
    real = httpx.AsyncClient

    class Client(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake.handle)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return fake


# --- WorkOS ---

class Accounts:
    """WorkOS 대신. 가입한 사람(사용자 ID → 이메일)과 계정 삭제 이벤트. reachable 이 False 면 WorkOS 에 닿지
    못한 것처럼 user_exists 가 None 을 돌려준다."""

    def __init__(self):
        self.emails: dict[str, str] = {}
        self.deleted: list[tuple[datetime, str]] = []
        self.reachable = True

    def add(self, user_id: str, email: str | None = None) -> str:
        self.emails[user_id] = email or f"{user_id}@test.example"
        return user_id

    def delete(self, user_id: str) -> None:
        self.emails.pop(user_id, None)
        self.deleted.append((datetime.now(timezone.utc), user_id))

    # 아래는 auth 의 바깥 호출과 같은 모양
    def user_exists(self, user_id):
        return (user_id in self.emails) if self.reachable else None

    def user_email(self, user_id):
        return self.emails.get(user_id)

    def user_id_for_email(self, email):
        return next((u for u, e in self.emails.items() if e == email), None)

    def deleted_users_since(self, start):
        return [(t, u) for t, u in self.deleted if t >= start]


@pytest.fixture(autouse=True)
def accounts(monkeypatch):
    fake = Accounts()
    for name in ("user_exists", "user_email", "user_id_for_email", "deleted_users_since"):
        monkeypatch.setattr(auth, name, getattr(fake, name))

    async def verify(self, token):
        if not token.startswith("user:"):
            return None
        return AccessToken(token=token, client_id="test", scopes=[], expires_at=int(time.time()) + 600,
                           subject=token.removeprefix("user:"))

    monkeypatch.setattr(auth.AuthKitVerifier, "verify_token", verify)
    return fake


# --- DB 와 서버 상태 ---

@pytest.fixture(autouse=True)
def fresh_state():
    """테이블을 비우고 서버가 메모리에 든 것을 처음 상태로 돌린다."""
    with db._connect() as conn:
        tables = [r["tablename"] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
        conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
    for path in glob.glob(server._BUFFER + "*"):
        os.remove(path)
    server._hits.clear()
    server._alive.clear()
    server._last_flush = time.monotonic()        # 보고가 저절로 옮겨지지 않게. 옮기기 테스트는 직접 부른다
    server._last_account_check = float("-inf")
    server._ACCEPT_TOKENLESS = False
    server._load_devices()


@pytest.fixture(scope="session")
def _app():
    # MCP 세션 관리자는 앱 하나에 한 번만 띄울 수 있어서 앱은 테스트 전체가 같이 쓴다
    with TestClient(server.mcp.streamable_http_app()) as client:
        yield client


@pytest.fixture
def client(_app):
    _app.cookies.clear()
    return _app
