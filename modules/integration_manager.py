"""Integration security and API key management for local plugin/app access."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable
from urllib.parse import urlparse

from flask import g, jsonify, request

from modules import license_manager
from modules.database import DB_PATH

INTEGRATION_SCOPE_STUDENTS_READ = "students:read"
INTEGRATION_SCOPE_EMAILS_SEND = "emails:send"
INTEGRATION_SCOPE_LICENSE_READ = "license:read"
INTEGRATION_SCOPE_PLUGINS_READ = "plugins:read"
INTEGRATION_SCOPE_KEYS_MANAGE = "keys:manage"

DEFAULT_SCOPES = [
    INTEGRATION_SCOPE_STUDENTS_READ,
    INTEGRATION_SCOPE_EMAILS_SEND,
    INTEGRATION_SCOPE_LICENSE_READ,
]

_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

_KCTM_SHARE_FILENAME = "kctm_integration_credentials.json"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_hwid(value: str | None) -> str:
    return str(value or "").strip().lower()


def _redact_hwid(value: str | None) -> str:
    normalized = _normalize_hwid(value)
    if not normalized:
        return ""
    if len(normalized) <= 10:
        return normalized[:4] + "…"
    return f"{normalized[:6]}…{normalized[-4:]}"


def _hwid_fingerprint(value: str | None) -> str:
    normalized = _normalize_hwid(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _license_context_snapshot() -> dict[str, Any]:
    saved = license_manager.get_saved_license() or {}
    context = license_manager.get_license_context() or {}
    current_hwid = license_manager.get_machine_fingerprint()
    stored_hwid = str(saved.get("machine_fingerprint") or "").strip()
    activated = bool(str(saved.get("license_key") or "").strip())
    machine_match = False
    if activated:
        machine_match = stored_hwid in {"", "*"} or _normalize_hwid(stored_hwid) == _normalize_hwid(current_hwid)

    reason = ""
    license_valid = bool(context.get("is_valid"))
    if not activated:
        reason = "not_activated"
    elif not machine_match:
        reason = "machine_mismatch"
    elif not license_valid:
        status = str(context.get("status") or "").strip().lower()
        message = str(context.get("message") or "").strip().lower()
        if status == "expired" or "expired" in message:
            reason = "license_expired"
        elif "revoked" in message or status == "revoked":
            reason = "license_revoked"
        else:
            reason = "service_unavailable"

    metadata = {}
    try:
        metadata = json.loads(str(saved.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        metadata = {}

    return {
        "ok": activated and license_valid and machine_match,
        "activated": activated,
        "license_valid": license_valid,
        "machine_match": machine_match,
        "reason": reason,
        "license_tier": str(metadata.get("license_tier") or metadata.get("tier") or "pro"),
        "expires_at": str(context.get("expires_at") or saved.get("expires_at") or ""),
        "issued_to": str(context.get("licensee") or saved.get("licensee") or saved.get("email") or ""),
        "checked_at": _now_utc_iso(),
        "machine_fingerprint": current_hwid,
        "stored_machine_fingerprint": stored_hwid,
        "status": str(context.get("status") or "unlicensed"),
        "message": str(context.get("message") or ""),
    }


def _license_denial_payload(reason: str, error: str, status_code: int, **extra: Any):
    payload = {"ok": False, "reason": reason, "error": error}
    payload.update(extra)
    return jsonify(payload), status_code


def _kctm_credentials_path() -> Path:
    return _default_shared_credentials_path()


def _is_local_base_url(base_url: str) -> bool:
    parsed = urlparse(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = str(parsed.hostname or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    ) as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
        temp_name = fh.name
    os.replace(temp_name, target)
    try:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    except Exception:
        pass


def get_kctm_credentials_path() -> str:
    return str(_kctm_credentials_path())


def get_kctm_credentials_last_write() -> str:
    target = _kctm_credentials_path()
    if not target.exists():
        return ""
    try:
        return datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return ""


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
            existing_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(integration_api_keys)").fetchall()
            }
            if "bound_hwid" not in existing_columns:
                conn.execute("ALTER TABLE integration_api_keys ADD COLUMN bound_hwid TEXT NOT NULL DEFAULT ''")
            if "rate_limit_per_minute" not in existing_columns:
                conn.execute(
                    "ALTER TABLE integration_api_keys ADD COLUMN rate_limit_per_minute INTEGER NOT NULL DEFAULT 120"
                )
            if "last_used_at" not in existing_columns:
                conn.execute("ALTER TABLE integration_api_keys ADD COLUMN last_used_at TEXT DEFAULT ''")
            if "last_used_ip" not in existing_columns:
                conn.execute("ALTER TABLE integration_api_keys ADD COLUMN last_used_ip TEXT DEFAULT ''")
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


def authorize_integration_request(
    required_scopes: Iterable[str] | None = None,
) -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
    needed_scopes = set(_normalize_scopes(required_scopes))

    if not is_local_request():
        log_audit(
            action="integration_access_denied",
            status_code=403,
            success=False,
            error="service_unavailable",
        )
        return None, _license_denial_payload(
            "service_unavailable",
            "Integration API accepts local requests only.",
            403,
        )

    auth_header = str(request.headers.get("Authorization") or "")
    if not auth_header.lower().startswith("bearer "):
        log_audit(
            action="integration_access_denied",
            status_code=401,
            success=False,
            error="invalid_api_key",
        )
        return None, _license_denial_payload(
            "invalid_api_key",
            "Missing bearer API key.",
            401,
        )

    raw_key = auth_header.split(" ", 1)[1].strip()
    if not raw_key:
        log_audit(
            action="integration_access_denied",
            status_code=401,
            success=False,
            error="invalid_api_key",
        )
        return None, _license_denial_payload(
            "invalid_api_key",
            "Missing bearer API key.",
            401,
        )

    record = _load_key_record(raw_key)
    if not record:
        log_audit(
            action="integration_access_denied",
            status_code=401,
            success=False,
            error="invalid_api_key",
        )
        return None, _license_denial_payload(
            "invalid_api_key",
            "Invalid integration API key.",
            401,
        )

    client_hwid = str(request.headers.get("X-Client-HWID") or "").strip()
    if not client_hwid:
        log_audit(
            action="integration_access_denied",
            status_code=400,
            success=False,
            key_id=record["id"],
            key_prefix=record["key_prefix"],
            error="missing_hwid",
            details={"hwid_fingerprint": ""},
        )
        return None, _license_denial_payload(
            "missing_hwid",
            "Missing X-Client-HWID header.",
            400,
        )

    normalized_client_hwid = _normalize_hwid(client_hwid)
    normalized_local_hwid = _normalize_hwid(local_hwid())
    normalized_bound_hwid = _normalize_hwid(record.get("bound_hwid") or "")

    if normalized_client_hwid != normalized_local_hwid or (
        normalized_bound_hwid and normalized_client_hwid != normalized_bound_hwid
    ):
        log_audit(
            action="integration_access_denied",
            status_code=403,
            success=False,
            key_id=record["id"],
            key_prefix=record["key_prefix"],
            error="machine_mismatch",
            details={"hwid_fingerprint": _hwid_fingerprint(client_hwid)},
        )
        return None, _license_denial_payload(
            "machine_mismatch",
            "License is bound to a different machine.",
            403,
        )

    granted_scopes = set(_normalize_scopes(record.get("scopes") or []))
    if needed_scopes and not needed_scopes.issubset(granted_scopes):
        missing = sorted(needed_scopes - granted_scopes)
        required_scope = missing[0] if missing else ""
        log_audit(
            action="integration_access_denied",
            status_code=403,
            success=False,
            key_id=record["id"],
            key_prefix=record["key_prefix"],
            error="missing_scope",
            details={"required_scope": required_scope, "missing_scopes": missing},
        )
        return None, _license_denial_payload(
            "missing_scope",
            "Missing required scope.",
            403,
            required_scope=required_scope,
            missing_scopes=missing,
        )

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
        return None, _license_denial_payload(
            "service_unavailable",
            "Rate limit exceeded.",
            429,
        )

    _update_key_usage(record["id"])
    record["client_hwid"] = normalized_client_hwid
    return record, None


def require_integration_auth(required_scopes: Iterable[str] | None = None):
    def _decorator(func):
        @wraps(func)
        def _wrapped(*args, **kwargs):
            record, denied = authorize_integration_request(required_scopes)
            if denied:
                return denied

            status = _license_context_snapshot()
            if not status.get("ok"):
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    key_id=record["id"],
                    key_prefix=record["key_prefix"],
                    error=str(status.get("reason") or "service_unavailable"),
                    details={"hwid_fingerprint": _hwid_fingerprint(record.get("client_hwid") or "")},
                )
                return _license_denial_payload(
                    str(status.get("reason") or "service_unavailable"),
                    {
                        "not_activated": "A valid license is required.",
                        "machine_mismatch": "License is bound to a different machine.",
                        "license_expired": "License has expired.",
                        "license_revoked": "License has been revoked.",
                    }.get(str(status.get("reason") or ""), "A valid license is required."),
                    403,
                    activated=bool(status.get("activated")),
                    license_valid=bool(status.get("license_valid")),
                    machine_match=bool(status.get("machine_match")),
                )

            if not is_instructor_station():
                log_audit(
                    action="integration_access_denied",
                    status_code=403,
                    success=False,
                    key_id=record["id"],
                    key_prefix=record["key_prefix"],
                    error="instructor_station_required",
                )
                return _license_denial_payload(
                    "service_unavailable",
                    "Integration API is available only on Instructor Station.",
                    403,
                )

            g.integration_client = record
            return func(*args, **kwargs)

        return _wrapped

    return _decorator


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


def _default_shared_credentials_path() -> Path:
    """Return default file path used to share integration credentials with KCTM."""
    override = str(os.getenv("STDYTIME_SHARED_CREDENTIALS_PATH") or "").strip()
    if override:
        return Path(override)

    local_appdata = str(os.getenv("LOCALAPPDATA") or "").strip()
    if local_appdata:
        return Path(local_appdata) / "Stdytime" / "integration" / _KCTM_SHARE_FILENAME

    return Path.cwd() / _KCTM_SHARE_FILENAME


def get_latest_active_api_key() -> dict[str, Any] | None:
    ensure_integration_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
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
                active,
                created_at,
                updated_at,
                last_used_at,
                last_used_ip
            FROM integration_api_keys
            WHERE active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if not row:
        return None

    scopes_json = str(row["scopes_json"] or "[]")
    try:
        scopes = json.loads(scopes_json)
    except (TypeError, ValueError):
        scopes = []

    return {
        "id": int(row["id"]),
        "name": str(row["name"] or ""),
        "key_prefix": str(row["key_prefix"] or ""),
        "key_hash": str(row["key_hash"] or ""),
        "key_salt": str(row["key_salt"] or ""),
        "scopes": _normalize_scopes(scopes),
        "bound_hwid": str(row["bound_hwid"] or ""),
        "rate_limit_per_minute": int(row["rate_limit_per_minute"] or 120),
        "active": bool(row["active"]),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "last_used_at": str(row["last_used_at"] or ""),
        "last_used_ip": str(row["last_used_ip"] or ""),
    }


def write_kctm_credentials_file(*, api_key: str, client_hwid: str, stdytime_base_url: str) -> dict[str, Any]:
    api_key = str(api_key or "").strip()
    client_hwid = str(client_hwid or "").strip()
    stdytime_base_url = str(stdytime_base_url or "").strip()

    path = get_kctm_credentials_path()
    if not api_key:
        return {"ok": False, "error": "api_key_missing", "path": path}
    if not client_hwid:
        return {"ok": False, "error": "client_hwid_missing", "path": path}
    if not stdytime_base_url:
        return {"ok": False, "error": "base_url_missing", "path": path}
    if not _is_local_base_url(stdytime_base_url):
        return {"ok": False, "error": "base_url_not_local", "path": path}

    payload = {
        "api_key": api_key,
        "client_hwid": client_hwid,
        "stdytime_base_url": stdytime_base_url,
    }

    target = _kctm_credentials_path()
    try:
        _atomic_write_json(target, payload)
        return {"ok": True, "path": str(target), "last_write": get_kctm_credentials_last_write()}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(target)}


def refresh_kctm_credentials_file(*, stdytime_base_url: str) -> dict[str, Any]:
    path = _kctm_credentials_path()
    if not path.exists():
        return {"ok": False, "error": "no_existing_credentials", "path": str(path)}

    try:
        current = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}

    api_key = str(current.get("api_key") or "").strip()
    client_hwid = str(current.get("client_hwid") or "").strip()
    if not api_key:
        return {"ok": False, "error": "api_key_missing", "path": str(path)}
    if not client_hwid:
        return {"ok": False, "error": "client_hwid_missing", "path": str(path)}

    return write_kctm_credentials_file(
        api_key=api_key,
        client_hwid=client_hwid,
        stdytime_base_url=stdytime_base_url,
    )


def share_api_key_with_kctm(
    created_key: dict[str, Any],
    *,
    stdytime_base_url: str = "",
) -> dict[str, Any]:
    """Persist a local credential bundle that KCTM can import automatically.

    The file is intended for same-machine app-to-app bootstrap only.
    """
    api_key = str(created_key.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "api_key_missing", "path": get_kctm_credentials_path()}

    scopes = _normalize_scopes(created_key.get("scopes") or [])
    required_for_kctm = {
        INTEGRATION_SCOPE_STUDENTS_READ,
        INTEGRATION_SCOPE_EMAILS_SEND,
        INTEGRATION_SCOPE_LICENSE_READ,
    }
    if not required_for_kctm.issubset(set(scopes)):
        return {
            "ok": False,
            "error": "missing_kctm_scopes",
            "path": get_kctm_credentials_path(),
            "missing_scopes": sorted(required_for_kctm - set(scopes)),
        }

    client_hwid = str(created_key.get("bound_hwid") or "").strip() or local_hwid()
    return write_kctm_credentials_file(
        api_key=api_key,
        client_hwid=client_hwid,
        stdytime_base_url=stdytime_base_url,
    )


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


