"""Local machine licensing utilities for single-install Stdytime deployments."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import sqlite3
import uuid
from datetime import date, datetime
from typing import Any

from modules.database import DB_PATH

try:
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None

LICENSE_TABLE = "app_license"
LOCAL_OWNER_ID = 1
DEFAULT_LICENSE_SECRET = "stdytime-local-license-secret"


def _license_secret() -> str:
    value = (os.getenv("STDYTIME_LICENSE_SECRET") or "").strip()
    return value or DEFAULT_LICENSE_SECRET


def _ensure_license_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LICENSE_TABLE} (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                license_key TEXT,
                licensee TEXT,
                email TEXT,
                issued_at TEXT,
                expires_at TEXT,
                machine_fingerprint TEXT,
                metadata_json TEXT DEFAULT '{{}}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


def _urlsafe_b64decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _registry_machine_guid() -> str:
    if winreg is None:
        return ""
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value or "").strip()
    except OSError:
        return ""


def get_machine_identity() -> dict[str, str]:
    markers = {
        "hostname": platform.node() or os.getenv("COMPUTERNAME", ""),
        "platform": platform.platform(),
        "processor": platform.processor() or os.getenv("PROCESSOR_IDENTIFIER", ""),
        "mac": f"{uuid.getnode():012x}",
        "machine_guid": _registry_machine_guid(),
    }
    return {key: str(value or "").strip() for key, value in markers.items()}


def get_machine_fingerprint() -> str:
    identity = get_machine_identity()
    joined = "|".join(f"{key}={identity[key]}" for key in sorted(identity))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def generate_license_key(payload: dict[str, Any]) -> str:
    payload_copy = dict(payload)
    payload_json = _canonical_payload(payload_copy)
    signature = hmac.new(
        _license_secret().encode("utf-8"),
        payload_json.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{_urlsafe_b64encode(payload_json.encode('utf-8'))}.{signature}"


def decode_license_key(license_key: str) -> tuple[dict[str, Any] | None, str | None]:
    raw = (license_key or "").strip()
    if not raw or "." not in raw:
        return None, "License key format is invalid."

    encoded_payload, supplied_signature = raw.rsplit(".", 1)
    try:
        payload_json = _urlsafe_b64decode(encoded_payload).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return None, "License key payload could not be decoded."

    expected_signature = hmac.new(
        _license_secret().encode("utf-8"),
        _canonical_payload(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None, "License key signature is invalid."

    return payload, None


def validate_license_payload(payload: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    fingerprint = str(payload.get("machine_fingerprint") or payload.get("fingerprint") or "").strip()
    expires_at = str(payload.get("expires_at") or "").strip()
    licensee = str(payload.get("licensee") or payload.get("customer") or "").strip()
    email = str(payload.get("email") or "").strip()
    issued_at = str(payload.get("issued_at") or datetime.utcnow().date().isoformat()).strip()

    if not fingerprint:
        return False, "License key is missing a machine fingerprint.", {}
    # A fingerprint of "*" is a universal/site license — machine binding is skipped.
    if fingerprint != "*" and fingerprint != get_machine_fingerprint():
        return False, "This license was issued for a different machine.", {}
    if not expires_at:
        return False, "License key is missing an expiration date.", {}

    try:
        expiry_date = date.fromisoformat(expires_at)
    except ValueError:
        return False, "License expiration date is invalid.", {}

    today = datetime.now().date()
    if expiry_date < today:
        return False, f"License expired on {expires_at}.", {
            "licensee": licensee,
            "email": email,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "machine_fingerprint": fingerprint,
        }

    return True, "License activated successfully.", {
        "licensee": licensee,
        "email": email,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "machine_fingerprint": fingerprint,
    }


def validate_license_key(license_key: str) -> tuple[bool, str, dict[str, Any]]:
    payload, error = decode_license_key(license_key)
    if error:
        return False, error, {}
    assert payload is not None
    return validate_license_payload(payload)


def get_saved_license() -> dict[str, Any] | None:
    _ensure_license_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {LICENSE_TABLE} WHERE id = 1"
        ).fetchone()
    return dict(row) if row else None


def activate_license(license_key: str) -> tuple[bool, str, dict[str, Any]]:
    valid, message, normalized = validate_license_key(license_key)
    if not normalized and not valid:
        return False, message, get_license_context()

    payload, _ = decode_license_key(license_key)
    metadata_json = json.dumps(payload or {}, sort_keys=True)
    now = datetime.now().isoformat()
    _ensure_license_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"""
            INSERT INTO {LICENSE_TABLE} (
                id, license_key, licensee, email,
                issued_at, expires_at, machine_fingerprint, metadata_json, updated_at
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                license_key = excluded.license_key,
                licensee = excluded.licensee,
                email = excluded.email,
                issued_at = excluded.issued_at,
                expires_at = excluded.expires_at,
                machine_fingerprint = excluded.machine_fingerprint,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                (license_key or "").strip(),
                normalized.get("licensee", ""),
                normalized.get("email", ""),
                normalized.get("issued_at", ""),
                normalized.get("expires_at", ""),
                normalized.get("machine_fingerprint", ""),
                metadata_json,
                now,
            ),
        )
        conn.commit()
    return True, message, get_license_context()


def remove_license() -> None:
    _ensure_license_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"DELETE FROM {LICENSE_TABLE} WHERE id = 1")
        conn.commit()


def get_license_context() -> dict[str, Any]:
    saved = get_saved_license()
    fingerprint = get_machine_fingerprint()
    identity = get_machine_identity()
    base = {
        "is_valid": False,
        "status": "unlicensed",
        "message": "Activate a license to unlock this installation.",
        "expires_at": "",
        "issued_at": "",
        "licensee": "",
        "email": "",
        "machine_fingerprint": fingerprint,
        "machine_name": identity.get("hostname", ""),
        "days_remaining": None,
        "default_home_endpoint": "dashboard",
        "has_license_key": bool(saved and saved.get("license_key")),
    }
    if not saved or not saved.get("license_key"):
        return base

    valid, message, normalized = validate_license_key(saved.get("license_key") or "")
    context = dict(base)
    context.update({
        "message": message,
        "licensee": saved.get("licensee") or normalized.get("licensee") or "",
        "email": saved.get("email") or normalized.get("email") or "",
        "issued_at": saved.get("issued_at") or normalized.get("issued_at") or "",
        "expires_at": saved.get("expires_at") or normalized.get("expires_at") or "",
        "has_license_key": True,
    })

    try:
        if context["expires_at"]:
            expiry_date = date.fromisoformat(context["expires_at"])
            context["days_remaining"] = (expiry_date - datetime.now().date()).days
    except ValueError:
        context["days_remaining"] = None

    if valid:
        context["is_valid"] = True
        context["status"] = "valid"
        context["message"] = f"License active until {context['expires_at']}."
        return context

    if context.get("expires_at"):
        try:
            expiry_date = date.fromisoformat(context["expires_at"])
            if expiry_date < datetime.now().date():
                context["status"] = "expired"
                context["message"] = f"Your license expired on {context['expires_at']}."
                return context
        except ValueError:
            pass

    context["status"] = "invalid"
    return context


class _LocalUser:
    """Minimal user object for single-machine licensed installs."""
    def __init__(self, user_id, email, licensee, expires_at, status):
        self.id = user_id
        self.email = email
        self.role = "admin"
        self.is_active = True
        self.must_change_password = False
        self.licensee = licensee
        self.license_expires_at = expires_at
        self.license_status = status

    def is_admin(self):
        return True


def get_local_user(license_context: dict[str, Any] | None = None):
    context = license_context or get_license_context()
    if not context.get("is_valid"):
        return None

    display_email = context.get("email") or context.get("licensee") or "Licensed Installation"
    return _LocalUser(
        user_id=LOCAL_OWNER_ID,
        email=display_email,
        licensee=context.get("licensee") or display_email,
        expires_at=context.get("expires_at"),
        status=context.get("status"),
    )
