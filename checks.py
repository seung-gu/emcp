"""들어온 값 검사와 요청 횟수 제한. 보드와 사람이 보낸 값은 모두 여기를 거쳐서 쓴다."""

import re
import time

import db.reports

# 보드가 만드는 값의 모양. 펌웨어와 맞춘 약속이다. 토큰은 32바이트 난수를 소문자 16진수로 쓴 64자,
# 등록 코드는 헷갈리는 0, 1, I, O 를 뺀 32글자 중 6자리다.
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# 기기가 보내는 로그 항목의 종류. 대시보드가 이 문자열을 이스케이프 없이 HTML 에 넣으므로
# 화이트리스트로 거른다 — 기기가 지어낸 문자열은 페이지에 닿지 못한다.
_LOG_KINDS = {"wifi", "http", "portal-new", "portal-lost", "portal-timeout", "full"}

_hits: dict[str, list[float]] = {}


def allowed(key: str, limit: int, per: int = 3600) -> bool:
    """key 로 per 초 안에 limit 번까지 허용한다. 메모리에만 세므로 재시작하면 처음부터 센다. 키가 지워지지
    않고 쌓이지만 보드와 사람 수가 적어서 그대로 둔다. 많아지면 오래된 키를 주기적으로 비운다."""
    now = time.monotonic()
    recent = [t for t in _hits.get(key, ()) if now - t < per]
    ok = len(recent) < limit
    if ok:
        recent.append(now)
    _hits[key] = recent
    return ok


def token(v) -> str | None:
    return v if isinstance(v, str) and _TOKEN_RE.fullmatch(v) else None


def code(v) -> str | None:
    """등록 코드. 사람이 넣은 대소문자, 공백, 하이픈은 무시한다."""
    if not isinstance(v, str):
        return None
    v = "".join(v.split()).replace("-", "").upper()
    return v if len(v) == 6 and all(c in CODE_CHARS for c in v) else None


def to_int(v) -> int | None:
    """숫자로 읽히면 int, 아니면 None. bool 은 숫자로 치지 않는다."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_float(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def to_bool(v) -> bool | None:
    """참/거짓만 통과. 0/1 이나 "true" 는 받지 않는다 — 기기가 보내는 건 JSON 불린이고,
    여기서 느슨하게 받으면 필드가 없는 것과 false 가 섞인다."""
    return v if isinstance(v, bool) else None


def tag(v) -> str | None:
    """펌웨어 버전처럼 기기가 만든 짧은 문자열. 대시보드가 이스케이프 없이 HTML 에 넣으므로
    태그가 될 수 있는 글자를 아예 통과시키지 않고, 길이도 자른다."""
    if not isinstance(v, str):
        return None
    v = "".join(c for c in v[:24] if c.isalnum() or c in "._-")
    return v or None


# db/reports.py 의 항목 타입마다 들어온 값을 거르는 함수.
CLEAN = {int: to_int, float: to_float, bool: to_bool, str: tag}


def clean_entry(e: dict) -> dict:
    """로그 한 항목에서 서버가 아는 키만 남긴다. 종류마다 실려 오는 필드가 달라서 없는 건
    그냥 빠진다 — 자리를 채우지 않는다."""
    kind = e.get("kind")
    out = {"kind": kind if kind in _LOG_KINDS else "unknown"}
    for k, kind in db.reports.LOG_FIELDS.items():
        v = CLEAN[kind](e.get(k))
        if v is not None:
            out[k] = v
    return out


def name(v: str) -> str | None:
    """사람이 정하는 도시 이름과 보드 이름. 앞뒤 공백을 떼고, 비었거나 40자를 넘으면 None."""
    v = v.strip()
    return v if 0 < len(v) <= 40 else None


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def email(v) -> str | None:
    """사람이 넣은 이메일. 앞뒤 공백을 떼고 소문자로 맞춘다. 모양이 아니면 None."""
    if not isinstance(v, str):
        return None
    v = v.strip().lower()
    return v if len(v) <= 254 and _EMAIL_RE.fullmatch(v) else None
