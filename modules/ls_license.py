"""
LemonSqueezy license validation for Stdytime.

Flow
----
1. User enters their LemonSqueezy license key in the /license page.
2. App calls ``activate_ls_license(key)`` which:
   a. Calls the LS Licenses API to validate the key and activate an instance.
   b. Persists the LS instance_id + status in app_license row.
3. On every app startup (and on demand) ``verify_ls_license()`` calls LS to
   confirm the instance is still active.
4. LemonSqueezy sends signed webhook events (subscription cancelled, etc.) to
   ``/webhooks/lemonsqueezy``. The webhook handler calls
   ``handle_ls_webhook_event()`` to update local state.

Environment variables required
-------------------------------
LS_API_KEY          Your LemonSqueezy API key (from LS dashboard → API).
LS_STORE_ID         Your LS store ID (numeric, e.g. "12345").
LS_WEBHOOK_SECRET   Secret you set when creating the LS webhook.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import requests

from modules.database import DB_PATH

logger = logging.getLogger(__name__)

LS_API_BASE = "https://api.lemonsqueezy.com/v1"
_TIMEOUT = 10  # seconds for all outbound LS API calls

# In-process validation cache — avoids calling LS on every HTTP request.
# Refreshed automatically when TTL expires or on explicit /license/verify.
_VALIDATION_CACHE_TTL_SECONDS = 4 * 3600  # 4 hours
_validation_cache: dict = {"valid": None, "message": "", "checked_at": 0.0}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    return (os.getenv("LS_API_KEY") or "").strip()


def _webhook_secret() -> str:
    return (os.getenv("LS_WEBHOOK_SECRET") or "").strip()


def _instance_name() -> str:
    """Human-readable activation label shown in LS dashboard."""
    import platform
    return (platform.node() or "stdytime-machine")[:64]


# ---------------------------------------------------------------------------
# Persistence helpers  (reuse the existing app_license row, id=1)
# ---------------------------------------------------------------------------

def _save_ls_fields(
    *,
    ls_instance_id: str,
    ls_status: str,
    ls_expires_at: str,
    licensee: str,
    email: str,
) -> None:
    """Upsert LemonSqueezy fields into the single app_license row."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        # Ensure columns exist (added by database.py migration, but guard here too)
        _ensure_ls_columns(conn)
        conn.execute(
            """
            INSERT INTO app_license (
                id, license_key, licensee, email,
                expires_at, ls_instance_id, ls_status, updated_at
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                licensee        = excluded.licensee,
                email           = excluded.email,
                expires_at      = excluded.expires_at,
                ls_instance_id  = excluded.ls_instance_id,
                ls_status       = excluded.ls_status,
                updated_at      = excluded.updated_at
            """,
            (licensee, email, ls_expires_at, ls_instance_id, ls_status, now),
        )
        conn.commit()


def _ensure_ls_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(app_license)").fetchall()}
    for col, definition in [
        ("ls_instance_id", "TEXT DEFAULT ''"),
        ("ls_status",      "TEXT DEFAULT ''"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE app_license ADD COLUMN {col} {definition}")
    conn.commit()


def _get_ls_row() -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM app_license WHERE id = 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# LemonSqueezy API calls
# ---------------------------------------------------------------------------

def _ls_headers() -> dict[str, str]:
    key = _api_key()
    if not key:
        raise ValueError(
            "LS_API_KEY environment variable is not set. "
            "Add it to your .env file to enable LemonSqueezy license validation."
        )
    return {
        "Authorization": f"Bearer {key}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }


def activate_ls_license(license_key: str) -> tuple[bool, str, dict[str, Any]]:
    """
    Call the LS Licenses API to activate *license_key* on this machine.
    Returns (success, message, context_dict).
    """
    license_key = license_key.strip()
    if not license_key:
        return False, "License key cannot be empty.", {}

    if not _api_key():
        return False, (
            "Server is missing LS_API_KEY. Contact the administrator."
        ), {}

    try:
        resp = requests.post(
            f"{LS_API_BASE}/licenses/activate",
            headers=_ls_headers(),
            json={
                "license_key": license_key,
                "instance_name": _instance_name(),
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("[ls_license] activate network error: %s", exc)
        return False, "Could not reach LemonSqueezy. Check your internet connection.", {}

    try:
        body = resp.json()
    except Exception:
        return False, f"LemonSqueezy returned an unexpected response (HTTP {resp.status_code}).", {}

    if resp.status_code not in (200, 201):
        error_msg = _extract_ls_error(body) or f"Activation failed (HTTP {resp.status_code})."
        logger.warning("[ls_license] activate failed: %s", body)
        return False, error_msg, {}

    # Parse successful response
    # LS activation response shape:
    # { "activated": true, "instance": { "id": "...", ... }, "license_key": { ... }, "meta": { ... } }
    meta = body.get("meta", {})
    instance = body.get("instance", {})
    lk_data = body.get("license_key", {})

    instance_id = str(instance.get("id") or "")
    ls_status = "active" if body.get("activated") else "inactive"
    expires_at = str(lk_data.get("expires_at") or meta.get("expires_at") or "")
    licensee = str(
        meta.get("customer_name") or lk_data.get("customer_name") or ""
    )
    email = str(
        meta.get("customer_email") or lk_data.get("customer_email") or ""
    )

    _save_ls_fields(
        ls_instance_id=instance_id,
        ls_status=ls_status,
        ls_expires_at=expires_at,
        licensee=licensee,
        email=email,
    )

    # Also store the raw license_key string for display
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE app_license SET license_key = ? WHERE id = 1",
            (license_key,),
        )
        conn.commit()

    message = f"License activated for {licensee or email}." if (licensee or email) else "License activated."
    context = get_ls_license_context()
    return True, message, context


def verify_ls_license() -> tuple[bool, str]:
    """
    Validate the stored LS instance against the LS API.
    Returns (is_valid, message).  Called at startup and on demand.
    """
    import time
    now = time.monotonic()
    cached = _validation_cache
    if (
        cached["valid"] is not None
        and (now - cached["checked_at"]) < _VALIDATION_CACHE_TTL_SECONDS
    ):
        return cached["valid"], cached["message"]

    return _verify_ls_license_uncached()


def _verify_ls_license_uncached() -> tuple[bool, str]:
    """Calls LS API unconditionally and updates the in-process cache."""
    import time
    row = _get_ls_row()
    if not row or not row.get("ls_instance_id"):
        valid, msg = False, "No LemonSqueezy license activated on this machine."
        _validation_cache.update({"valid": valid, "message": msg, "checked_at": time.monotonic()})
        return valid, msg

    if not _api_key():
        # No API key: fall back to trusting what is stored locally
        stored_status = row.get("ls_status", "")
        if stored_status == "active":
            valid, msg = True, "License valid (offline check; LS_API_KEY not set)."
        else:
            valid, msg = False, "License inactive (offline check; LS_API_KEY not set)."
        _validation_cache.update({"valid": valid, "message": msg, "checked_at": time.monotonic()})
        return valid, msg

    license_key = (row.get("license_key") or "").strip()
    instance_id = (row.get("ls_instance_id") or "").strip()

    try:
        resp = requests.post(
            f"{LS_API_BASE}/licenses/validate",
            headers=_ls_headers(),
            json={
                "license_key": license_key,
                "instance_id": instance_id,
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("[ls_license] validate network error: %s", exc)
        # Fail open: if we can't reach LS, trust local state
        stored_status = row.get("ls_status", "")
        valid = stored_status == "active"
        msg = f"License check skipped (network error): {exc}"
        # Don't cache network failures — retry sooner (1 min)
        _validation_cache.update({"valid": valid, "message": msg, "checked_at": time.monotonic() - _VALIDATION_CACHE_TTL_SECONDS + 60})
        return valid, msg

    try:
        body = resp.json()
    except Exception:
        msg = f"Unexpected response from LemonSqueezy (HTTP {resp.status_code})."
        _validation_cache.update({"valid": False, "message": msg, "checked_at": time.monotonic()})
        return False, msg

    valid = bool(body.get("valid"))
    ls_status = "active" if valid else "inactive"

    # Update local status from API response
    meta = body.get("meta", {})
    lk_data = body.get("license_key", {})
    expires_at = str(lk_data.get("expires_at") or meta.get("expires_at") or row.get("expires_at") or "")
    licensee = str(meta.get("customer_name") or row.get("licensee") or "")
    email = str(meta.get("customer_email") or row.get("email") or "")

    _save_ls_fields(
        ls_instance_id=instance_id,
        ls_status=ls_status,
        ls_expires_at=expires_at,
        licensee=licensee,
        email=email,
    )

    if valid:
        msg = "License is active (verified with LemonSqueezy)."
        _validation_cache.update({"valid": True, "message": msg, "checked_at": time.monotonic()})
        return True, msg
    error_msg = _extract_ls_error(body) or "License is not valid."
    _validation_cache.update({"valid": False, "message": error_msg, "checked_at": time.monotonic()})
    return False, error_msg


def deactivate_ls_license() -> tuple[bool, str]:
    """
    Deactivate this machine's instance in LS (called when user removes license).
    """
    row = _get_ls_row()
    if not row or not row.get("ls_instance_id"):
        return True, "No active instance to deactivate."

    license_key = (row.get("license_key") or "").strip()
    instance_id = (row.get("ls_instance_id") or "").strip()

    if not _api_key() or not license_key or not instance_id:
        return True, "Instance deactivated locally."

    try:
        resp = requests.post(
            f"{LS_API_BASE}/licenses/deactivate",
            headers=_ls_headers(),
            json={"license_key": license_key, "instance_id": instance_id},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return True, "License deactivated with LemonSqueezy."
        return False, f"LS deactivation returned HTTP {resp.status_code}."
    except requests.RequestException as exc:
        logger.warning("[ls_license] deactivate error: %s", exc)
        return False, f"Network error during deactivation: {exc}"


def get_ls_license_context() -> dict[str, Any]:
    """Return a dict suitable for passing to templates / license_status API."""
    row = _get_ls_row()
    if not row or not row.get("ls_instance_id"):
        return {
            "is_valid": False,
            "status": "unlicensed",
            "message": "No LemonSqueezy license activated. Enter your license key below.",
            "licensee": "",
            "email": "",
            "expires_at": "",
            "days_remaining": None,
            "has_license_key": False,
            "ls_status": "",
        }

    ls_status = row.get("ls_status", "")
    is_valid = ls_status == "active"
    expires_at = str(row.get("expires_at") or "")
    days_remaining: int | None = None
    if expires_at:
        try:
            # LS returns ISO-8601 with timezone; normalize to date
            exp_str = expires_at[:10]
            from datetime import date
            expiry_date = date.fromisoformat(exp_str)
            days_remaining = (expiry_date - datetime.now().date()).days
            if days_remaining < 0:
                is_valid = False
                ls_status = "expired"
        except ValueError:
            pass

    return {
        "is_valid": is_valid,
        "status": ls_status,
        "message": (
            f"License active — expires {expires_at[:10]}." if is_valid
            else f"License {ls_status}. Please renew or re-activate."
        ),
        "licensee": row.get("licensee", ""),
        "email": row.get("email", ""),
        "expires_at": expires_at,
        "days_remaining": days_remaining,
        "has_license_key": bool(row.get("license_key")),
        "ls_status": ls_status,
        "default_home_endpoint": "dashboard",
    }


# ---------------------------------------------------------------------------
# Webhook event handler
# ---------------------------------------------------------------------------

def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify the X-Signature header LemonSqueezy sends with every webhook.
    LS signs the raw request body with HMAC-SHA256 using your webhook secret.
    """
    secret = _webhook_secret()
    if not secret:
        logger.warning("[ls_license] LS_WEBHOOK_SECRET not set — skipping signature check.")
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


def handle_ls_webhook_event(payload: dict[str, Any]) -> str:
    """
    Process a verified LemonSqueezy webhook event.
    Returns a log-friendly description of the action taken.
    """
    event_name = payload.get("meta", {}).get("event_name", "")
    data = payload.get("data", {})
    attrs = data.get("attributes", {})

    logger.info("[ls_license] webhook event: %s", event_name)

    # --- license_key_activated / license_key_created -----------------------
    if event_name in ("license_key_created", "license_key_activated"):
        instance_id = str(data.get("id") or "")
        ls_status = "active" if attrs.get("status") == "active" else "inactive"
        expires_at = str(attrs.get("expires_at") or "")
        customer = payload.get("meta", {}).get("customer_name", "")
        email = payload.get("meta", {}).get("customer_email", "")
        _save_ls_fields(
            ls_instance_id=instance_id,
            ls_status=ls_status,
            ls_expires_at=expires_at,
            licensee=customer,
            email=email,
        )
        return f"License activated via webhook for {email or customer}."

    # --- subscription_updated -----------------------------------------------
    if event_name == "subscription_updated":
        status = attrs.get("status", "")
        ends_at = str(attrs.get("ends_at") or attrs.get("renews_at") or "")
        ls_status = "active" if status in ("active", "on_trial") else status
        row = _get_ls_row()
        if row:
            _save_ls_fields(
                ls_instance_id=row.get("ls_instance_id", ""),
                ls_status=ls_status,
                ls_expires_at=ends_at or row.get("expires_at", ""),
                licensee=row.get("licensee", ""),
                email=row.get("email", ""),
            )
        return f"Subscription updated — new status: {ls_status}."

    # --- subscription_cancelled / subscription_expired / license_key_expired -
    if event_name in (
        "subscription_cancelled",
        "subscription_expired",
        "license_key_expired",
        "license_key_disabled",
    ):
        row = _get_ls_row()
        if row:
            _save_ls_fields(
                ls_instance_id=row.get("ls_instance_id", ""),
                ls_status="inactive",
                ls_expires_at=row.get("expires_at", ""),
                licensee=row.get("licensee", ""),
                email=row.get("email", ""),
            )
        return f"License deactivated via webhook event: {event_name}."

    return f"Unhandled webhook event '{event_name}' — ignored."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_ls_error(body: dict) -> str:
    """Pull a human-readable error out of various LS error shapes."""
    if "error" in body:
        return str(body["error"])
    errors = body.get("errors", [])
    if errors:
        first = errors[0]
        return str(first.get("detail") or first.get("title") or first)
    return ""
