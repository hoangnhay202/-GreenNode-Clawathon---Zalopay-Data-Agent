"""PostgreSQL-backed persistent store — replaces diskcache for all app state.

Tables: app_tokens, oauth_tokens, users, rss_feeds, job_configs
Connection pool is shared across all callers (ThreadedConnectionPool, max 10 conns).
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import Json

logger = logging.getLogger(__name__)

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        url = os.getenv("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL env var not set")
        _pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
        logger.info("pg_store: connection pool created (max=10)")
    return _pool


def _conn():
    return _get_pool().getconn()


def _put(conn) -> None:
    _get_pool().putconn(conn)


# ── App tokens (Bot Framework / Graph API — client_credentials) ──────────────

def get_app_token(scope: str) -> str | None:
    """Return cached app token if not expired, else None."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT access_token FROM app_tokens WHERE scope = %s AND expires_at > NOW()",
                (scope,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        _put(conn)


def set_app_token(scope: str, access_token: str, ttl_seconds: int) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_tokens (scope, access_token, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (scope) DO UPDATE
                  SET access_token = EXCLUDED.access_token,
                      expires_at   = EXCLUDED.expires_at
                """,
                (scope, access_token, expires_at),
            )
        conn.commit()
    finally:
        _put(conn)


def delete_app_token(scope: str) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_tokens WHERE scope = %s", (scope,))
        conn.commit()
    finally:
        _put(conn)


# ── OAuth delegated tokens (per user) ────────────────────────────────────────

def get_oauth_tokens(user_aad_id: str) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token_data FROM oauth_tokens WHERE user_aad_id = %s",
                (user_aad_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        _put(conn)


def set_oauth_tokens(user_aad_id: str, token_data: dict) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_tokens (user_aad_id, token_data, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_aad_id) DO UPDATE
                  SET token_data = EXCLUDED.token_data,
                      updated_at = NOW()
                """,
                (user_aad_id, Json(token_data)),
            )
        conn.commit()
    finally:
        _put(conn)


# ── Users ────────────────────────────────────────────────────────────────────

def get_user(user_aad_id: str) -> dict | None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_aad_id, user_name, email FROM users WHERE user_aad_id = %s",
                (user_aad_id,),
            )
            row = cur.fetchone()
            if row:
                return {"aad_id": row[0], "name": row[1], "email": row[2]}
            return None
    finally:
        _put(conn)


def upsert_user(user_aad_id: str, user_name: str | None = None, email: str | None = None) -> None:
    """Insert user on first seen; update name/email only when a non-None value is provided."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (user_aad_id, user_name, email, create_date)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_aad_id) DO UPDATE
                  SET user_name = COALESCE(EXCLUDED.user_name, users.user_name),
                      email     = COALESCE(EXCLUDED.email,     users.email)
                """,
                (user_aad_id, user_name, email),
            )
        conn.commit()
    finally:
        _put(conn)


# ── RSS feeds (per user) ─────────────────────────────────────────────────────

def get_rss_feeds(user_id: str) -> list:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT feeds FROM rss_feeds WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return row[0] if row else []
    finally:
        _put(conn)


def set_rss_feeds(user_id: str, feeds: list) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rss_feeds (user_id, feeds, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                  SET feeds      = EXCLUDED.feeds,
                      updated_at = NOW()
                """,
                (user_id, Json(feeds)),
            )
        conn.commit()
    finally:
        _put(conn)


# ── Scheduled job configs ────────────────────────────────────────────────────

def save_job_config(job_id: str, job_type: str, config: dict) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_configs (job_id, job_type, config, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (job_id) DO UPDATE
                  SET job_type   = EXCLUDED.job_type,
                      config     = EXCLUDED.config,
                      updated_at = NOW()
                """,
                (job_id, job_type, Json(config)),
            )
        conn.commit()
    finally:
        _put(conn)


def delete_job_config(job_id: str) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_configs WHERE job_id = %s", (job_id,))
        conn.commit()
    finally:
        _put(conn)


def get_all_job_configs() -> list[dict]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT job_id, job_type, config FROM job_configs ORDER BY updated_at")
            return [{"job_id": r[0], "job_type": r[1], **r[2]} for r in cur.fetchall()]
    finally:
        _put(conn)


# ── Generic KV store (session/transient data with optional TTL) ───────────────

def kv_get(key: str) -> Any | None:
    """Return value for key, or None if not found / expired."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM kv_store WHERE key = %s AND (expires_at IS NULL OR expires_at > NOW())",
                (key,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        _put(conn)


def kv_set(key: str, value: Any, expire_seconds: int | None = None) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expire_seconds) if expire_seconds else None
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv_store (key, value, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO UPDATE
                  SET value      = EXCLUDED.value,
                      expires_at = EXCLUDED.expires_at
                """,
                (key, Json(value), expires_at),
            )
        conn.commit()
    finally:
        _put(conn)


def kv_delete(key: str) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
        conn.commit()
    finally:
        _put(conn)


def kv_keys_by_prefix(prefix: str) -> list[str]:
    """Return all non-expired keys that start with prefix."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key FROM kv_store WHERE key LIKE %s AND (expires_at IS NULL OR expires_at > NOW())",
                (prefix + "%",),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        _put(conn)
