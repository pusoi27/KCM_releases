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
import time
from datetime import datetime, timezone
from typing import Any

import requests

from modules.database import DB_PATH

logger = logging.getLogger(__name__)

LS_API_BASE = "https://api.lemonsqueezy.com/v1"
_TIMEOUT = 10  # seconds for all outbound LS API calls

# In-process validation cache — avoids calling LS on every HTTP request.
# Refreshed automatically when TTL expires or on explicit /license/verify.
_validation_cache: dict = {"valid": None, "message": "", "checked_at": 0.0}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _api_key() -> str:
    return (os.getenv("LS_API_KEY") or "").strip()


def _api_only_mode() -> bool:
    """When true, do not rely on webhook/local fallback for validation decisions."""
    return (os.getenv("LS_API_ONLY_MODE", "true") or "").strip().lower() == "true"


def is_api_only_mode() -> bool:
    """Public helper for routes/templates to check API-only mode state."""
    return _api_only_mode()


def _cache_ttl_seconds() -> int:
    """Validation cache TTL, clamped to 1-4 hours."""
    raw = (os.getenv("LS_CACHE_TTL_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 4 * 3600
    except ValueError:
        value = 4 * 3600
    return max(3600, min(4 * 3600, value))


def get_cache_ttl_seconds() -> int:
    """Public helper exposing effective validation cache TTL."""
    return _cache_ttl_seconds()


def _grace_window_seconds() -> int:
    """Grace window for temporary LS/network outages (default: 24 hours)."""
    raw = (os.getenv("LS_GRACE_HOURS") or "").strip()
    try:
        hours = int(raw) if raw else 24
    except ValueError:
        hours = 24
    return max(0, hours * 3600)


def get_grace_hours() -> int:
    """Public helper exposing effective network-grace window in hours."""
    return int(_grace_window_seconds() / 3600)


def get_nav_badge_data() -> dict[str, Any]:
    """Telemetry for tiny top-nav badge: color, age, cache remaining, grace mode."""
    row = _get_ls_row() or {}
    ctx = get_ls_license_context()

    age_seconds = _age_seconds_from_iso(row.get("ls_last_verified_at") or "")
    age_text = _humanize_seconds(age_seconds) if age_seconds is not None else "n/a"

    ttl = _cache_ttl_seconds()
    checked_at = float(_validation_cache.get("checked_at") or 0.0)
    if checked_at > 0:
        elapsed = max(0, int(time.monotonic() - checked_at))
        cache_remaining = max(0, ttl - elapsed)
    else:
        cache_remaining = 0

    cache_text = _humanize_seconds(cache_remaining)
    cache_pct = int((cache_remaining / ttl) * 100) if ttl > 0 else 0

    message = str(_validation_cache.get("message") or "")
    grace_mode = bool("grace window" in message.lower() and ctx.get("is_valid"))

    if not ctx.get("has_license_key"):
        tone = "danger"
        label = "LS no key"
    elif grace_mode:
        tone = "warning"
        label = "LS grace"
    elif ctx.get("is_valid"):
        tone = "success"
        label = "LS ok"
    else:
        tone = "danger"
        label = "LS invalid"

    return {
        "visible": True,
        "tone": tone,
        "label": label,
        "age_text": age_text,
        "cache_remaining_text": cache_text,
        "cache_remaining_pct": max(0, min(100, cache_pct)),
        "grace_mode": grace_mode,
        "tooltip": (
            f"Last LS check age: {age_text} | "
            f"Cache TTL remaining: {cache_text} | "
            f"Grace mode: {'yes' if grace_mode else 'no'}"
        ),
    }


def _parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _humanize_seconds(seconds: int) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def _age_seconds_from_iso(value: str | None) -> int | None:
    parsed = _parse_iso_utc(value)
    if not parsed:
        return None
    return max(0, int((_now_utc() - parsed).total_seconds()))


def _within_grace(last_verified_at: str | None) -> bool:
    if _grace_window_seconds() <= 0:
        return False
    parsed = _parse_iso_utc(last_verified_at)
    if not parsed:
        return False
    age_seconds = (_now_utc() - parsed).total_seconds()
    return age_seconds <= _grace_window_seconds()


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
    ls_last_verified_at: str | None = None,
    activation_limit: int = 0,
    activation_usage: int = 0,
) -> None:
    """Upsert LemonSqueezy fields into the single app_license row."""
    now = datetime.now(timezone.utc).isoformat()
    verified_at = ls_last_verified_at or ""
    with sqlite3.connect(DB_PATH) as conn:
        # Ensure columns exist (added by database.py migration, but guard here too)
        _ensure_ls_columns(conn)
        conn.execute(
            """
            INSERT INTO app_license (
                id, license_key, licensee, email,
                expires_at, ls_instance_id, ls_status, ls_last_verified_at,
                activation_limit, activation_usage, updated_at
            ) VALUES (1, '', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                licensee        = excluded.licensee,
                email           = excluded.email,
                expires_at      = excluded.expires_at,
                ls_instance_id  = excluded.ls_instance_id,
                ls_status       = excluded.ls_status,
                ls_last_verified_at = CASE
                    WHEN excluded.ls_last_verified_at <> '' THEN excluded.ls_last_verified_at
                    ELSE app_license.ls_last_verified_at
                END,
                activation_limit = excluded.activation_limit,
                activation_usage = excluded.activation_usage,
                updated_at      = excluded.updated_at
            """,
            (licensee, email, ls_expires_at, ls_instance_id, ls_status, verified_at, activation_limit, activation_usage, now),
        )
        conn.commit()


def _ensure_ls_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(app_license)").fetchall()}
    for col, definition in [
        ("ls_instance_id", "TEXT DEFAULT ''"),
        ("ls_status",      "TEXT DEFAULT ''"),
        ("ls_last_verified_at", "TEXT DEFAULT ''"),
        ("activation_limit", "INTEGER DEFAULT 0"),
        ("activation_usage", "INTEGER DEFAULT 0"),
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
    activation_limit = int(lk_data.get("activation_limit") or 0)
    activation_usage = int(lk_data.get("activation_usage") or 0)

    _save_ls_fields(
        ls_instance_id=instance_id,
        ls_status=ls_status,
        ls_expires_at=expires_at,
        licensee=licensee,
        email=email,
        activation_limit=activation_limit,
        activation_usage=activation_usage,
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


def verify_ls_license(force: bool = False) -> tuple[bool, str]:
    """
    Validate the stored LS instance against the LS API.
    Returns (is_valid, message).  Called at startup and on demand.
    """
    import time
    now = time.monotonic()
    cached = _validation_cache
    if (
        not force
        and
        cached["valid"] is not None
        and (now - cached["checked_at"]) < _cache_ttl_seconds()
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
        if _api_only_mode():
            valid, msg = False, "LS_API_KEY is required in API-only mode."
            _validation_cache.update({"valid": valid, "message": msg, "checked_at": time.monotonic()})
            return valid, msg

        # Non-API-only mode: fall back to trusting what is stored locally
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
        stored_status = row.get("ls_status", "")
        within_grace = _within_grace(row.get("ls_last_verified_at") or "")
        valid = stored_status == "active" and within_grace
        if valid:
            msg = (
                "LemonSqueezy unreachable; allowing temporary access within grace window. "
                f"({exc})"
            )
        else:
            msg = (
                "LemonSqueezy unreachable and grace window expired. "
                "Please re-check when internet is available."
            )
        # Retry relatively soon on network failures (1 minute)
        _validation_cache.update({
            "valid": valid,
            "message": msg,
            "checked_at": time.monotonic() - _cache_ttl_seconds() + 60,
        })
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
    activation_limit = int(lk_data.get("activation_limit") or 0)
    activation_usage = int(lk_data.get("activation_usage") or 0)

    _save_ls_fields(
        ls_instance_id=instance_id,
        ls_status=ls_status,
        ls_expires_at=expires_at,
        licensee=licensee,
        email=email,
        ls_last_verified_at=_now_utc().isoformat() if valid else None,
        activation_limit=activation_limit,
        activation_usage=activation_usage,
    )

    if valid:
        msg = "License is active (verified with LemonSqueezy)."
        _validation_cache.update({"valid": True, "message": msg, "checked_at": time.monotonic()})
        return True, msg
    error_msg = _extract_ls_error(body) or "License is not valid."
    _validation_cache.update({"valid": False, "message": error_msg, "checked_at": time.monotonic()})
    return False, error_msg


def should_force_revalidate_request(*, method: str, path: str, endpoint: str | None = None) -> bool:
    """
    Decide whether this request should bypass cache and force LS revalidation.
    Sensitive cases: exports, admin/settings paths, and write operations.
    """
    m = (method or "").upper()
    p = (path or "").lower()
    e = (endpoint or "").lower()

    if "/export" in p or p.startswith("/exports"):
        return True

    if any(token in p for token in ("/admin", "/settings", "/instructor-profile", "/instructor/profile")):
        return True

    if any(token in e for token in ("admin", "setting", "export")):
        return True

    if m in {"POST", "PUT", "PATCH", "DELETE"}:
        # Exclude high-frequency timer/session writes to avoid noisy LS traffic.
        if p.startswith("/api/sessions") or p.startswith("/api/timer"):
            return False
        return True

    return False


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
        "ls_last_verified_at": row.get("ls_last_verified_at", ""),
        "api_only_mode": _api_only_mode(),
        "cache_ttl_seconds": _cache_ttl_seconds(),
        "grace_hours": int(_grace_window_seconds() / 3600),
        "activation_limit": row.get("activation_limit", 0),
        "activation_usage": row.get("activation_usage", 0),
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


def _mask_email(email: str) -> str:
    """Return a privacy-safe masked version: o***@domain.com."""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"


def validate_email_matches_license(email: str) -> str | None:
    """
    Check that *email* matches the customer email stored in the activated
    LemonSqueezy license.  Returns an error message string when the emails
    differ, or ``None`` when the check passes (or there is no LS license yet
    to compare against).
    """
    row = _get_ls_row()
    if not row or not row.get("ls_instance_id"):
        # No LS license activated on this machine yet — nothing to compare.
        return None

    license_email = (row.get("email") or "").strip().lower()
    if not license_email:
        # License activated but no customer email recorded — allow.
        return None

    if email.strip().lower() == license_email:
        return None  # Match — all good.

    masked = _mask_email(license_email)
    return (
        f"The email you entered does not match the email used to purchase this license "
        f"({masked}). Please enter the email you used when buying Stdytime on LemonSqueezy."
    )


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
