"""Integration security and API key management for local plugin/app access."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Iterable

from flask import g, jsonify, request

from modules import license_manager
from modules.database import DB_PATH

INTEGRATION_SCOPE_STUDENTS_READ = "students:read"
INTEGRATION_SCOPE_EMAILS_SEND = "emails:send"
INTEGRATION_SCOPE_PLUGINS_READ = "plugins:read"
INTEGRATION_SCOPE_KEYS_MANAGE = "keys:manage"

DEFAULT_SCOPES = [
    INTEGRATION_SCOPE_STUDENTS_READ,
    INTEGRATION_SCOPE_EMAILS_SEND,
]

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_integration_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS integration_api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    key_prefix TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    key_salt TEXT NOT NULL,
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    bound_hwid TEXT NOT NULL DEFAULT '',
                    rate_limit_per_minute INTEGER NOT NULL DEFAULT 120,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TEXT DEFAULT '',
                    last_used_ip TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS integration_rate_limits (
                    key_id INTEGER NOT NULL,
                    window_start TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (key_id, window_start),
                    FOREIGN KEY (key_id) REFERENCES integration_api_keys(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS integration_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_id INTEGER,
                    key_prefix TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    remote_addr TEXT NOT NULL DEFAULT '',
                    status_code INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (key_id) REFERENCES integration_api_keys(id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_integration_api_keys_active
                ON integration_api_keys(active)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_integration_audit_created
                ON integration_audit_log(created_at DESC)
                """
            )
            conn.commit()

        _SCHEMA_READY = True


def _normalize_scopes(scopes: Iterable[str] | None) -> list[str]:
    ordered: list[str] = []
    seen = set()
    for raw in scopes or []:
        scope = str(raw or "").strip().lower()
        if not scope or scope in seen:
            continue
        seen.add(scope)
        ordered.append(scope)
    return ordered


def _hash_api_key(raw_key: str, salt: str) -> str:
    payload = f"{salt}:{raw_key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def local_hwid() -> str:
    return license_manager.get_machine_fingerprint()


def _loopback_candidate(value: str | None) -> bool:
    token = str(value or "").strip().lower()
    if not token:
        return False
    if token in {"127.0.0.1", "::1", "localhost"}:
        return True
    if token.startswith("::ffff:127."):
        return True
    return token.startswith("127.")


def is_local_request() -> bool:
    direct_addr = request.remote_addr
    if _loopback_candidate(direct_addr):
        return True

    if request.access_route:
        for hop in request.access_route:
            if _loopback_candidate(hop):
                return True

    return False


def is_instructor_station() -> bool:
    status = g.get("license_status") or {}
    activation_limit = int(status.get("activation_limit") or 0)
    role = str(status.get("station_role") or "").strip().lower()

    # Single-activation licenses act as a single local owner box.
    if activation_limit < 2:
        return True

    return role == "instructor"


def create_api_key(
    *,
    name: str,
    scopes: Iterable[str] | None = None,
    rate_limit_per_minute: int = 120,
    bound_hwid: str | None = None,
) -> dict[str, Any]:
    ensure_integration_schema()

    normalized_name = str(name or "").strip() or "Local Integration Client"
    normalized_scopes = _normalize_scopes(scopes) or list(DEFAULT_SCOPES)
    normalized_rate = max(10, int(rate_limit_per_minute or 120))

    raw_key = f"stk_{secrets.token_urlsafe(32)}"
    salt = secrets.token_hex(16)
    key_hash = _hash_api_key(raw_key, salt)
    key_prefix = raw_key[:14]

    effective_hwid = str(bound_hwid or "").strip() or local_hwid()
    now = _now_utc_iso()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO integration_api_keys (
                name, key_prefix, key_hash, key_salt, scopes_json,
                bound_hwid, rate_limit_per_minute, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                normalized_name,
                key_prefix,
                key_hash,
                salt,
                json.dumps(normalized_scopes),
                effective_hwid,
                normalized_rate,
                now,
                now,
            ),
        )
        key_id = int(cur.lastrowid)
        conn.commit()

    return {
        "id": key_id,
        "name": normalized_name,
        "api_key": raw_key,
        "key_prefix": key_prefix,
        "scopes": normalized_scopes,
        "bound_hwid": effective_hwid,
        "rate_limit_per_minute": normalized_rate,
        "created_at": now,
    }


def list_api_keys() -> list[dict[str, Any]]:
    ensure_integration_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id,
                name,
                key_prefix,
                scopes_json,
                bound_hwid,
                rate_limit_per_minute,
                active,
                created_at,
                updated_at,
                last_used_at,
                last_used_ip
            FROM integration_api_keys
            ORDER BY id DESC
            """
        ).fetchall()

    result = []
    for row in rows:
        scopes_json = str(row["scopes_json"] or "[]")
        try:
            scopes = json.loads(scopes_json)
        except (TypeError, ValueError):
            scopes = []
        result.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"] or ""),
                "key_prefix": str(row["key_prefix"] or ""),
                "scopes": _normalize_scopes(scopes),
                "bound_hwid": str(row["bound_hwid"] or ""),
                "rate_limit_per_minute": int(row["rate_limit_per_minute"] or 120),
                "active": bool(row["active"]),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "last_used_at": str(row["last_used_at"] or ""),
                "last_used_ip": str(row["last_used_ip"] or ""),
            }
        )
    return result


def revoke_api_key(key_id: int) -> bool:
    ensure_integration_schema()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE integration_api_keys
            SET active = 0, updated_at = ?
            WHERE id = ?
            """,
            (_now_utc_iso(), key_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _consume_rate_limit(*, key_id: int, max_requests_per_minute: int) -> tuple[bool, int]:
    now = datetime.now(timezone.utc)
    window_start = now.replace(second=0, microsecond=0).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        existing = cur.execute(
            """
            SELECT request_count
            FROM integration_rate_limits
            WHERE key_id = ? AND window_start = ?
            """,
            (key_id, window_start),
        ).fetchone()

        current_count = int(existing[0]) if existing else 0
        if current_count >= max_requests_per_minute:
            return False, current_count

        if existing:
            cur.execute(
                """
                UPDATE integration_rate_limits
                SET request_count = request_count + 1
                WHERE key_id = ? AND window_start = ?
                """,
                (key_id, window_start),
            )
        else:
            cur.execute(
                """
                INSERT INTO integration_rate_limits (key_id, window_start, request_count)
                VALUES (?, ?, 1)
                """,
                (key_id, window_start),
            )

        # Keep table tidy.
        cleanup_before = (now - timedelta(minutes=3)).replace(second=0, microsecond=0).isoformat()
        cur.execute(
            """
            DELETE FROM integration_rate_limits
            WHERE window_start < ?
            """,
            (cleanup_before,),
        )

        conn.commit()

    return True, current_count + 1


def log_audit(
    *,
    action: str,
    status_code: int,
    success: bool,
    key_id: int | None = None,
    key_prefix: str = "",
    error: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    ensure_integration_schema()

    payload = json.dumps(details or {}, sort_keys=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO integration_audit_log (
                key_id, key_prefix, action, method, path, remote_addr,
                status_code, success, error, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_id,
                str(key_prefix or ""),
                str(action or "unknown"),
                request.method,
                request.path,
                str(request.remote_addr or ""),
                int(status_code),
                1 if success else 0,
                str(error or ""),
                payload,
                _now_utc_iso(),
            ),
        )
        conn.commit()


def _update_key_usage(key_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE integration_api_keys
            SET
                last_used_at = ?,
                last_used_ip = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (_now_utc_iso(), str(request.remote_addr or ""), _now_utc_iso(), key_id),
        )
        conn.commit()


def _load_key_record(raw_api_key: str) -> dict[str, Any] | None:
    ensure_integration_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id,
                name,
                key_prefix,
                key_hash,
                key_salt,
                scopes_json,
                bound_hwid,
                rate_limit_per_minute,
                active
            FROM integration_api_keys
            WHERE active = 1
            """
        ).fetchall()

    for row in rows:
        expected = str(row["key_hash"] or "")
        salt = str(row["key_salt"] or "")
        if not expected or not salt:
            continue
        if secrets.compare_digest(_hash_api_key(raw_api_key, salt), expected):
            scopes_raw = str(row["scopes_json"] or "[]")
            try:
                scopes = json.loads(scopes_raw)
            except (TypeError, ValueError):
                scopes = []
            return {
                "id": int(row["id"]),
                "name": str(row["name"] or ""),
                "key_prefix": str(row["key_prefix"] or ""),
                "scopes": _normalize_scopes(scopes),
                "bound_hwid": str(row["bound_hwid"] or ""),
                "rate_limit_per_minute": int(row["rate_limit_per_minute"] or 120),
                "active": bool(row["active"]),
            }

    return None


def require_integration_auth(required_scopes: Iterable[str] | None = None):
    needed_scopes = set(_normalize_scopes(required_scopes))

    def _decorator(func):
        @wraps(func)
        def _wrapped(*args, **kwargs):
            if not is_local_request():
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    error="non_local_request",
                )
                return jsonify({"error": "Integration API accepts local requests only."}), 403

            status = g.get("license_status") or {}
            if not status.get("is_valid"):
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    error="license_invalid",
                )
                return jsonify({"error": "A valid license is required."}), 403

            if not is_instructor_station():
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    error="instructor_station_required",
                )
                return jsonify({"error": "Integration API is available only on Instructor Station."}), 403

            auth_header = str(request.headers.get("Authorization") or "")
            if not auth_header.lower().startswith("bearer "):
                log_audit(
                    action="integration_access_denied",
                    status_code=401,
                    success=False,
                    error="missing_bearer_token",
                )
                return jsonify({"error": "Missing bearer API key."}), 401

            raw_key = auth_header.split(" ", 1)[1].strip()
            if not raw_key:
                log_audit(
                    action="integration_access_denied",
                    status_code=401,
                    success=False,
                    error="empty_bearer_token",
                )
                return jsonify({"error": "Missing bearer API key."}), 401

            record = _load_key_record(raw_key)
            if not record:
                log_audit(
                    action="integration_access_denied",
                    status_code=401,
                    success=False,
                    error="invalid_api_key",
                )
                return jsonify({"error": "Invalid integration API key."}), 401

            client_hwid = str(request.headers.get("X-Client-HWID") or "").strip()
            if not client_hwid:
                log_audit(
                    action="integration_access_denied",
                    status_code=401,
                    success=False,
                    key_id=record["id"],
                    key_prefix=record["key_prefix"],
                    error="missing_client_hwid",
                )
                return jsonify({"error": "Missing X-Client-HWID header."}), 401

            expected_local_hwid = local_hwid()
            bound_hwid = str(record.get("bound_hwid") or "")

            if client_hwid != expected_local_hwid or (bound_hwid and client_hwid != bound_hwid):
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    key_id=record["id"],
                    key_prefix=record["key_prefix"],
                    error="hwid_mismatch",
                )
                return jsonify({"error": "HWID mismatch."}), 403

            granted_scopes = set(_normalize_scopes(record.get("scopes") or []))
            if needed_scopes and not needed_scopes.issubset(granted_scopes):
                missing = sorted(needed_scopes - granted_scopes)
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    key_id=record["id"],
                    key_prefix=record["key_prefix"],
                    error="scope_missing",
                    details={"missing_scopes": missing},
                )
                return jsonify({"error": "Missing required scope.", "missing_scopes": missing}), 403

            allowed, current_count = _consume_rate_limit(
                key_id=record["id"],
                max_requests_per_minute=int(record.get("rate_limit_per_minute") or 120),
            )
            if not allowed:
                log_audit(
                    action="integration_access_denied",
                    status_code=429,
                    success=False,
                    key_id=record["id"],
                    key_prefix=record["key_prefix"],
                    error="rate_limited",
                    details={"requests_in_window": current_count},
                )
                return jsonify({"error": "Rate limit exceeded."}), 429

            _update_key_usage(record["id"])
            g.integration_client = record
            return func(*args, **kwargs)

        return _wrapped

    return _decorator
