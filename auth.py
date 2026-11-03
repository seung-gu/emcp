"""WorkOS AuthKit 로그인.

/mcp 에서는 MCP 클라이언트(Claude 등)가 AuthKit 에서 받아 온 토큰을 검사한다. 대시보드에서는 브라우저가
토큰을 받아 오지 못하므로 이 서버가 대신 로그인시키고, 사용자 ID 를 봉인한 쿠키를 브라우저에 맡긴다.
두 경로 모두 WorkOS 사용자 ID 로 사람을 구분한다.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from functools import cache

import jwt
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from workos import WorkOSClient, WorkOSError
from workos.session import seal_data, unseal_data

AUTH_ISSUER = os.environ.get("MCP_AUTH_ISSUER")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8080")
RESOURCE = f"{PUBLIC_URL}/mcp"   # 이 서버의 MCP 주소. 토큰의 aud 가 이 값과 같아야 한다


# --- /mcp: MCP 클라이언트가 보내는 AuthKit 토큰 검사 ---
# MCP 인증 (OAuth 2.1): https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization

class AuthKitVerifier(TokenVerifier):
    """우리 AuthKit 이 이 서버 앞으로 서명한 토큰만 받는다. 가입은 누구나 할 수 있고, 무엇을 볼 수 있는지는
    device_access 가 정한다."""

    def __init__(self):
        self.jwks = jwt.PyJWKClient(f"{AUTH_ISSUER}/oauth2/jwks")

    async def verify_token(self, token: str) -> AccessToken | None:
        # 인증 미들웨어는 모든 경로의 Bearer 값을 여기로 넘긴다. JWT 모양이 아니면 우리 토큰이 아니다
        if token.count(".") != 2:
            return None
        try:
            # 헤더의 kid 에 맞는 공개 키를 고른다. 키 목록을 받아 오는 호출이 블로킹이라 스레드에서 돌린다
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=AUTH_ISSUER,
                audience=RESOURCE,
                # PyJWT 는 exp 가 없으면 만료 검사를 건너뛴다. exp, sub, client_id 가 없는 토큰은 거절한다
                options={"require": ["exp", "sub", "client_id"]},
            )
        except jwt.PyJWTError as e:
            logging.getLogger(__name__).warning("token rejected: %s", e)
            return None
        logging.getLogger(__name__).info("token accepted: %s", claims["sub"])
        # subject 에 사용자 ID 를 넣는다. 도구는 이 값으로 호출한 사람을 알고, SDK 는 이 값으로 세션 주인을 확인한다
        return AccessToken(token=token, client_id=claims["client_id"], scopes=[],
                           expires_at=claims["exp"], subject=claims["sub"])


def fastmcp_options() -> dict:
    """FastMCP 에 넘길 인증 설정. MCP_AUTH_ISSUER 가 없으면 예외를 내서 서버가 뜨지 않게 한다. 인증 없이
    /mcp 를 열면 누구나 쓰기 도구를 부를 수 있기 때문이다."""
    if not AUTH_ISSUER:
        raise RuntimeError("MCP_AUTH_ISSUER is not set: refusing to serve /mcp without sign-in")
    return {
        "token_verifier": AuthKitVerifier(),
        "auth": AuthSettings(issuer_url=AUTH_ISSUER, resource_server_url=RESOURCE),
    }


# --- 대시보드 로그인: AuthKit 으로 로그인시키고 봉인한 세션 쿠키를 준다 ---
# OAuth 2.1 코드 흐름: https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/
# PKCE: https://www.rfc-editor.org/rfc/rfc7636

_COOKIE_PASSWORD = os.environ.get("WORKOS_COOKIE_PASSWORD", "")
SIGN_IN_HOURS = 12

try:
    Fernet(_COOKIE_PASSWORD)
except ValueError:  # 없거나 Fernet 키가 아니면 로그인과 세션 확인이 모두 500 이 된다
    logging.getLogger(__name__).warning("WORKOS_COOKIE_PASSWORD is not a Fernet key: dashboard sign-in fails")


class SignInError(Exception):
    """로그인을 끝낼 수 없을 때 낸다. 이 브라우저에서 시작한 로그인이 없거나, 다른 로그인의 응답인 경우다."""


@cache
def _workos() -> WorkOSClient:
    # client ID 는 어느 WorkOS 애플리케이션인지 알리고, API 키는 이 서버가 그 애플리케이션임을 증명한다
    return WorkOSClient(api_key=os.environ["WORKOS_API_KEY"], client_id=os.environ["WORKOS_CLIENT_ID"])


def start_sign_in() -> tuple[str, str]:
    """WorkOS 로그인 주소와, 브라우저가 돌아올 때까지 쿠키에 맡겨 둘 봉인한 state·PKCE 검증값을 돌려준다."""
    pkce = _workos().user_management.get_authorization_url_with_pkce(
        redirect_uri=f"{PUBLIC_URL}/auth/callback", provider="authkit"
    )
    started = {"state": pkce["state"], "code_verifier": pkce["code_verifier"]}
    return pkce["url"], seal_data(started, _COOKIE_PASSWORD)


def finish_sign_in(started_sealed: str, code: str | None, state: str | None) -> str:
    """WorkOS 가 돌려준 인가 코드로 사용자를 확인하고, SIGN_IN_HOURS 동안 쓸 봉인한 세션을 돌려준다."""
    try:
        started = unseal_data(started_sealed, _COOKIE_PASSWORD)
    except InvalidToken as e:
        raise SignInError("이 브라우저에서 시작한 로그인이 없습니다. /dashboard 에서 다시 시작하세요.") from e
    # 세션 쿠키를 로그인 쿠키 자리에 넣어도 복호화는 되지만 state 가 없다. 그래서 .get 으로 읽는다
    if not code or not state or state != started.get("state"):
        raise SignInError("이 브라우저가 시작한 로그인이 아닙니다. /dashboard 에서 다시 시작하세요.")
    try:
        result = _workos().user_management.authenticate_with_code_pkce(
            code=code, code_verifier=started["code_verifier"]
        )
    except WorkOSError as e:  # 이미 쓴 코드(뒤로 가기, 새로 고침), 만료된 코드, WorkOS 장애
        logging.getLogger(__name__).warning("sign-in code exchange failed: %s", e)
        raise SignInError("WorkOS 가 로그인을 끝내지 못했습니다. /dashboard 에서 다시 시작하세요.") from e
    return seal_data({"until": time.time() + SIGN_IN_HOURS * 3600, "sub": result.user.id}, _COOKIE_PASSWORD)


def deleted_users_since(start: datetime) -> list[tuple[datetime, str]]:
    """start 이후 WorkOS 에서 지워진 계정의 (지운 시각, 사용자 ID), 오래된 것부터. 한 번에 100개까지라
    더 있으면 다음 확인 때 이어서 가져온다."""
    utc = start.astimezone(timezone.utc)
    page = _workos().events.list_events(
        events=["user.deleted"],
        range_start=utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z",   # WorkOS 가 받는 모양
        order="asc",
        limit=100,
    )
    return [(e.created_at, e.data.id) for e in page.data]


def session_user(session_sealed: str) -> str | None:
    """세션 쿠키에 든 사용자 ID. 쿠키가 없거나, 이 서버가 봉인한 쿠키가 아니거나, 만료됐으면 None."""
    try:
        session = unseal_data(session_sealed, _COOKIE_PASSWORD)
    except InvalidToken:
        return None
    if session.get("until", 0) <= time.time():   # 로그인 쿠키에는 until 이 없어서 여기서 걸린다
        return None
    return session.get("sub")
