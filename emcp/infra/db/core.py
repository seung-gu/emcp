"""DB 연결과 마이그레이션 적용, 서버가 기억해 둘 값(app_state)."""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

from .migrations import MIGRATIONS

_migrated = False


def connect() -> psycopg.Connection:
    global _migrated
    # prepare_threshold=None: Neon 의 pooled 주소(PgBouncer) 뒤에서는 연결이 요청마다 바뀔 수 있어서
    # 서버 쪽에 준비해 둔 문장을 믿을 수 없다.
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, prepare_threshold=None)
    if not _migrated:
        _migrate(conn)
        _migrated = True
    return conn


def _migrate(conn: psycopg.Connection) -> None:
    """MIGRATIONS 중 아직 적용하지 않은 것만 실행한다. 앱 두 개(emcp, emcp-led)가 같은 DB 를 쓰므로
    동시에 켜져도 한쪽만 적용하도록 트랜잭션 잠금을 건다. PgBouncer 뒤에서는 세션 잠금이 풀릴 수 있다."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7311)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        done = conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_migrations").fetchone()["v"]
        for version, statement in enumerate(MIGRATIONS[done:], start=done + 1):
            conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))



def get_state(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES (%s, %s)"
                     " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
