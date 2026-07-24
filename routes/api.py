# routes/api.py
from flask import jsonify, request, g
from modules import student_manager, assistant_manager, timer_manager, auth_manager, license_manager
from modules import server_cache
from modules import scanner_sync
from modules.email_manager import get_email_manager, render_branded_email_shell, resolve_center_name
from modules import instructor_profile_manager
from modules.database import (
    DB_PATH,
    GDRIVE_SYNC_PATH,
    sync_to_gdrive,
    is_station_mailbox_mode_enabled,
    get_station_runtime_config,
)
from modules.utils import duration_seconds, time_now
from datetime import datetime, timedelta, time
import base64
import binascii
from email.message import EmailMessage
import ipaddress
import mimetypes
import os
import smtplib
import sqlite3
import json
import traceback
import requests
import time
from routes.auth import require_login, require_admin, require_feature


CHECKOUT_COOLDOWN_SECONDS = 60
STAFF_DUTY_MAX_DAILY_SECONDS = 6 * 60 * 60
BRIDGE_GET_TIMEOUT_SECONDS = 4
BRIDGE_POST_TIMEOUT_SECONDS = 5
BRIDGE_OFFLINE_COOLDOWN_SECONDS = 20
_BRIDGE_RETRY_AFTER_MONOTONIC = 0.0


def _runtime_station_mode() -> str:
    return str(get_station_runtime_config().get("station_mode") or "").strip().lower() or "instructor_server"


def _runtime_backup_mode() -> str:
    return str(get_station_runtime_config().get("backup_mode") or "").strip().lower() or "instructor_snapshots_only"


def _runtime_instructor_api_base() -> str:
    return str(get_station_runtime_config().get("instructor_api_base_url") or "").strip().rstrip("/")


def _runtime_pairing_token() -> str:
    return str(get_station_runtime_config().get("station_pairing_token") or "").strip()


def _scanner_api_client_enabled() -> bool:
    return _runtime_station_mode() == "scanner_api_client"


def _build_bridge_headers() -> dict:
    token = _runtime_pairing_token()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["X-Stdytime-Pairing-Token"] = token
    return headers


def _bridge_url(path: str) -> str:
    base = _runtime_instructor_api_base()
    route = str(path or "").strip()
    if not route.startswith("/"):
        route = "/" + route
    return f"{base}{route}"


def _bridge_json_response(response):
    try:
        body = response.json()
    except Exception:
        body = {"error": f"Bridge returned non-JSON response (HTTP {response.status_code})."}
    return body, int(response.status_code)


def _mark_scanner_bridge_offline(reason: str) -> None:
    global _BRIDGE_RETRY_AFTER_MONOTONIC
    _BRIDGE_RETRY_AFTER_MONOTONIC = time.monotonic() + max(1, int(BRIDGE_OFFLINE_COOLDOWN_SECONDS))
    try:
        g.scanner_bridge_fallback = True
        g.scanner_bridge_fallback_reason = str(reason or "bridge_unreachable")
    except Exception:
        pass


def _bridge_is_temporarily_offline() -> bool:
    return time.monotonic() < float(_BRIDGE_RETRY_AFTER_MONOTONIC)


def _bridge_forward_get(path: str):
    if not _scanner_api_client_enabled():
        return None

    if _bridge_is_temporarily_offline():
        _mark_scanner_bridge_offline("cooldown")
        return None

    url = _bridge_url(path)
    if not url.startswith("http"):
        _mark_scanner_bridge_offline("missing_instructor_api_url")
        return None
    try:
        response = requests.get(url, headers=_build_bridge_headers(), timeout=(2, BRIDGE_GET_TIMEOUT_SECONDS))
    except requests.RequestException as exc:
        _mark_scanner_bridge_offline(f"request_error:{exc}")
        return None
    if int(response.status_code) >= 500:
        _mark_scanner_bridge_offline(f"upstream_http_{int(response.status_code)}")
        return None
    global _BRIDGE_RETRY_AFTER_MONOTONIC
    _BRIDGE_RETRY_AFTER_MONOTONIC = 0.0
    body, code = _bridge_json_response(response)
    return jsonify(body), code


def _bridge_forward_post(path: str, payload: dict | None = None):
    if not _scanner_api_client_enabled():
        return None

    if _bridge_is_temporarily_offline():
        _mark_scanner_bridge_offline("cooldown")
        return None

    url = _bridge_url(path)
    if not url.startswith("http"):
        _mark_scanner_bridge_offline("missing_instructor_api_url")
        return None
    try:
        response = requests.post(url, headers=_build_bridge_headers(), json=(payload or {}), timeout=(2, BRIDGE_POST_TIMEOUT_SECONDS))
    except requests.RequestException as exc:
        _mark_scanner_bridge_offline(f"request_error:{exc}")
        return None
    if int(response.status_code) >= 500:
        _mark_scanner_bridge_offline(f"upstream_http_{int(response.status_code)}")
        return None
    global _BRIDGE_RETRY_AFTER_MONOTONIC
    _BRIDGE_RETRY_AFTER_MONOTONIC = 0.0
    body, code = _bridge_json_response(response)
    return jsonify(body), code


def _bridge_request_is_pairing_authorized() -> bool:
    expected = _runtime_pairing_token()
    if not expected:
        return False
    received = str(request.headers.get("X-Stdytime-Pairing-Token") or "").strip()
    return bool(received and received == expected)


def _bridge_pairing_auth_error(message: str = "Invalid or missing pairing token."):
    return jsonify({"error": message}), 401


def _request_is_loopback_client() -> bool:
    remote_addr = str(request.remote_addr or "").strip().lower()
    if not remote_addr:
        return False

    if remote_addr in {"127.0.0.1", "::1", "localhost"}:
        return True

    try:
        return bool(ipaddress.ip_address(remote_addr).is_loopback)
    except ValueError:
        return False


def _bridge_single_station_remote_block_response():
    return jsonify({
        "error": "Remote bridge/database access is disabled for single-station licenses.",
        "hint": "Upgrade to a multi-machine license (2+ activations) and pair stations to allow remote bridge traffic.",
    }), 403


def _enforce_bridge_remote_access_policy():
    """Block remote machine bridge/database access when this is a single-station license."""
    status = g.get("license_status") or {}
    activation_limit = int(status.get("activation_limit") or 0)

    # Multi-machine licenses can allow remote bridge requests (token-gated).
    if activation_limit >= 2:
        return None

    # Single-station license: only same-machine loopback may call bridge routes.
    if _request_is_loopback_client():
        return None

    return _bridge_single_station_remote_block_response()

# Global helper cache for performance (UI helpers)


def _trace_column3(event: str, **fields) -> None:
    """Lightweight terminal trace for checked-out column debugging."""
    if fields:
        details = " ".join(f"{key}={fields[key]!r}" for key in sorted(fields))
        print(f"[column3-trace] {event} {details}")
    else:
        print(f"[column3-trace] {event}")


def _trace_staff_duty(event: str, **fields) -> None:
    """Terminal trace helper for Staff on Duty modal/list/toggle flows."""
    if fields:
        details = " ".join(f"{key}={fields[key]!r}" for key in sorted(fields))
        print(f"[staff-duty-trace] {event} {details}")
    else:
        print(f"[staff-duty-trace] {event}")


def _safe_fromisoformat(raw_value: str):
    token = str(raw_value or '').strip()
    if not token:
        return None
    try:
        return datetime.fromisoformat(token)
    except Exception:
        return None


def _assistant_closed_seconds_today(cur: sqlite3.Cursor, assistant_id: int, day_iso: str) -> int:
    row = cur.execute(
        """
        SELECT COALESCE(SUM(COALESCE(duration, 0)), 0)
        FROM assistant_sessions
        WHERE assistant_id = ?
          AND DATE(start_time) = ?
          AND end_time IS NOT NULL
        """,
        (assistant_id, day_iso),
    ).fetchone()
    return int((row[0] if row else 0) or 0)


def _auto_close_stale_assistant_sessions(conn: sqlite3.Connection) -> int:
    """Close stale/open assistant sessions to enforce daily 6-hour duty limit."""
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, assistant_id, start_time
        FROM assistant_sessions
        WHERE end_time IS NULL
        ORDER BY id ASC
        """,
        (),
    ).fetchall()
    if not rows:
        return 0

    closed_count = 0
    for session_id, assistant_id, start_time in rows:
        start_dt = _safe_fromisoformat(start_time)
        if not start_dt:
            end_dt = datetime.now()
            cur.execute(
                "UPDATE assistant_sessions SET end_time=?, duration=?, sync_synced = 0 WHERE id=?",
                (end_dt.isoformat(), 0, session_id),
            )
            closed_count += int(cur.rowcount or 0)
            continue

        now_dt = datetime.now(start_dt.tzinfo) if start_dt.tzinfo else datetime.now()
        raw_elapsed = max(0, int((now_dt - start_dt).total_seconds()))

        start_day_iso = start_dt.date().isoformat()
        today_iso = now_dt.date().isoformat()
        closed_today = _assistant_closed_seconds_today(cur, int(assistant_id), start_day_iso)
        remaining_for_start_day = max(0, STAFF_DUTY_MAX_DAILY_SECONDS - closed_today)

        end_of_start_day = datetime.combine(start_dt.date(), time(23, 59, 59), tzinfo=start_dt.tzinfo)
        day_boundary_elapsed = max(0, int((end_of_start_day - start_dt).total_seconds()))
        elapsed_cap = min(STAFF_DUTY_MAX_DAILY_SECONDS, remaining_for_start_day, day_boundary_elapsed)
        allowed_elapsed = min(raw_elapsed, elapsed_cap)
        should_close = (
            start_day_iso != today_iso
            or raw_elapsed >= STAFF_DUTY_MAX_DAILY_SECONDS
            or remaining_for_start_day <= 0
            or allowed_elapsed < raw_elapsed
        )

        if not should_close:
            continue

        end_dt = start_dt + timedelta(seconds=allowed_elapsed)
        if end_dt > now_dt:
            end_dt = now_dt
        final_duration = max(0, int((end_dt - start_dt).total_seconds()))

        cur.execute(
            "UPDATE assistant_sessions SET end_time=?, duration=?, sync_synced = 0 WHERE id=?",
            (end_dt.isoformat(), final_duration, session_id),
        )
        closed_count += int(cur.rowcount or 0)

    if closed_count:
        conn.commit()
    return closed_count


def _force_reset_all_open_assistant_sessions(conn: sqlite3.Connection) -> int:
    """Force-close all open assistant sessions (used for manual duty reset)."""
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, start_time
        FROM assistant_sessions
        WHERE end_time IS NULL
        ORDER BY id ASC
        """,
        (),
    ).fetchall()

    if not rows:
        return 0

    now_dt = datetime.now()
    closed_count = 0
    for session_id, start_time in rows:
        start_dt = _safe_fromisoformat(start_time)
        if not start_dt:
            duration = 0
            end_dt = now_dt
        else:
            end_dt = datetime.now(start_dt.tzinfo) if start_dt.tzinfo else now_dt
            duration = max(0, int((end_dt - start_dt).total_seconds()))
            duration = min(duration, STAFF_DUTY_MAX_DAILY_SECONDS)
            end_dt = start_dt + timedelta(seconds=duration)

        cur.execute(
            "UPDATE assistant_sessions SET end_time=?, duration=?, sync_synced = 0 WHERE id=?",
            (end_dt.isoformat(), duration, session_id),
        )
        closed_count += int(cur.rowcount or 0)

    if closed_count:
        conn.commit()
    return closed_count


def _push_cloud_backup_after_staff_change(aid: int, action: str) -> None:
    """Best-effort immediate cloud push after staff duty writes.

    This reduces stale cross-machine duty state when a second machine starts
    before the 9-minute background sync cycle runs.
    """
    try:
        if _runtime_backup_mode() == "instructor_snapshots_only":
            _trace_staff_duty("snapshot_mode_skip_direct_cloud_push", aid=aid, action=action)
            return

        if is_station_mailbox_mode_enabled():
            _trace_staff_duty("mailbox_mode_enabled_skip_direct_cloud_push", aid=aid, action=action)
            return

        if not GDRIVE_SYNC_PATH:
            _trace_staff_duty("cloud_push_skipped_no_path", aid=aid, action=action)
            return

        pushed = sync_to_gdrive(
            DB_PATH,
            GDRIVE_SYNC_PATH,
            retries=1,
            retry_delay=1,
            silent=True,
        )
        _trace_staff_duty("cloud_push_result", aid=aid, action=action, pushed=bool(pushed))
    except Exception as exc:
        _trace_staff_duty("cloud_push_error", aid=aid, action=action, error=str(exc))


def _push_cloud_backup_after_student_change(student_id: int, action: str, source: str) -> None:
    """Best-effort immediate cloud push after main-class checkin/checkout writes."""
    try:
        if _runtime_backup_mode() == "instructor_snapshots_only":
            _trace_column3("snapshot_mode_skip_direct_cloud_push", sid=student_id, action=action, source=source)
            return

        if is_station_mailbox_mode_enabled():
            _trace_column3("mailbox_mode_enabled_skip_direct_cloud_push", sid=student_id, action=action, source=source)
            return

        if not GDRIVE_SYNC_PATH:
            _trace_column3("cloud_push_skipped_no_path", sid=student_id, action=action, source=source)
            return

        pushed = sync_to_gdrive(
            DB_PATH,
            GDRIVE_SYNC_PATH,
            retries=1,
            retry_delay=1,
            silent=True,
        )
        _trace_column3("cloud_push_result", sid=student_id, action=action, source=source, pushed=bool(pushed))
    except Exception as exc:
        _trace_column3("cloud_push_error", sid=student_id, action=action, source=source, error=str(exc))


def _students_list_cache_key() -> str:
    return server_cache.STUDENTS_LIST_CACHE_KEY


def _has_photo_blob(student_row) -> bool:
    """Return True when the student row contains a non-empty photo blob."""
    blob, _ = _extract_photo_blob_and_mime(student_row)
    return bool(blob)


def _extract_photo_blob_and_mime(student_row):
    """Return (photo_blob_bytes|None, photo_mime) from heterogeneous student row shapes."""
    if not student_row:
        return None, ''

    # Known row shapes used across the app:
    # - get_student():      ... photo_blob(19), photo_mime(20), ...
    # - get_all_students(): ... total_study_minutes(19), photo_blob(20), photo_mime(21)
    candidate_pairs = [(20, 21), (19, 20), (21, 22)]

    def _as_blob(value):
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, (bytes, bytearray)) and len(value) > 0:
            return bytes(value)
        return None

    for blob_idx, mime_idx in candidate_pairs:
        if len(student_row) <= blob_idx:
            continue
        blob = _as_blob(student_row[blob_idx])
        if not blob:
            continue
        mime = ''
        if len(student_row) > mime_idx:
            mime = str(student_row[mime_idx] or '').strip()
        return blob, mime

    # Fallback for legacy/unknown tuple layouts: scan right-to-left for first non-empty bytes-like value.
    for idx in range(len(student_row) - 1, -1, -1):
        blob = _as_blob(student_row[idx])
        if not blob:
            continue
        mime = ''
        if idx + 1 < len(student_row):
            next_val = student_row[idx + 1]
            if isinstance(next_val, str):
                candidate_mime = next_val.strip()
                if '/' in candidate_mime:
                    mime = candidate_mime
        return blob, mime

    return None, ''


def _photo_data_uri(student_row) -> str:
    """Return a data URI for a student blob photo, or empty string if absent."""
    blob, mime = _extract_photo_blob_and_mime(student_row)
    if not blob:
        return ''
    mime = mime or 'image/png'
    encoded = base64.b64encode(blob).decode('ascii')
    return f'data:{mime};base64,{encoded}'

def _assistants_profile_cache_key() -> str:
    return server_cache.ASSISTANTS_PROFILE_LIST_CACHE_KEY

def _assistants_duty_cache_key() -> str:
    return server_cache.ASSISTANTS_DUTY_LIST_CACHE_KEY


def _subjects_from_student_row(student_row) -> list:
    """Return a safe subjects list from get_all_students() rows."""
    if not student_row:
        return []

    # Current get_all_students() shape stores subjects_json at index 17.
    raw_subjects = student_row[17] if len(student_row) > 17 else None
    if raw_subjects:
        try:
            parsed = json.loads(raw_subjects)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item or '').strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            # Legacy/malformed values can appear as delimited text; normalize gracefully.
            text = str(raw_subjects).strip()
            for sep in ('|', ';', ','):
                if sep in text:
                    tokens = [piece.strip() for piece in text.split(sep) if piece and piece.strip()]
                    if tokens:
                        return tokens

            if text:
                return [text]

    return [student_row[2]] if len(student_row) > 2 and student_row[2] else []


def _total_study_minutes_from_student_row(student_row) -> int:
    """Return total planned study minutes for a student row from get_all_students()."""
    if not student_row:
        return 30

    try:
        total_minutes = int(student_row[19]) if len(student_row) > 19 and student_row[19] is not None else 0
    except (TypeError, ValueError):
        total_minutes = 0
    if total_minutes > 0:
        return total_minutes

    # Fallback: sum subject_minutes_json when total column is missing/invalid.
    raw_minutes = student_row[18] if len(student_row) > 18 else None
    if raw_minutes:
        try:
            parsed = json.loads(raw_minutes)
            if isinstance(parsed, list):
                minute_values = [max(0, int(item)) for item in parsed if item is not None]
                summed = sum(minute_values)
                if summed > 0:
                    return summed
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # Final fallback: subjects count * 30 minutes.
    subjects = _subjects_from_student_row(student_row)
    return max(30, len(subjects) * 30)


def _schedule_entries_from_student_row(student_row) -> list:
    """Return normalized scheduled day/time entries for get_all_students() rows."""
    if not student_row:
        return []

    entries = []
    seen_days = set()

    raw_schedule_json = student_row[23] if len(student_row) > 23 else None
    if raw_schedule_json:
        try:
            parsed = json.loads(raw_schedule_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = []
        if isinstance(parsed, list):
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                day = str(entry.get('day') or '').strip()
                time = str(entry.get('time') or '').strip()
                if not day or day in seen_days:
                    continue
                seen_days.add(day)
                entries.append({'day': day, 'time': time})
                if len(entries) >= 6:
                    return entries

    for day_idx, time_idx in ((13, 14), (15, 16), (24, 25), (26, 27), (28, 29), (30, 31)):
        day = str(student_row[day_idx] or '').strip() if len(student_row) > day_idx else ''
        time = str(student_row[time_idx] or '').strip() if len(student_row) > time_idx else ''
        if not day or day in seen_days:
            continue
        seen_days.add(day)
        entries.append({'day': day, 'time': time})
        if len(entries) >= 6:
            break

    return entries

def _format_checkout_timestamp(value: str) -> str:
    """Format ISO-ish timestamps for human-readable emails."""
    if not value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d %I:%M:%S %p")
    except Exception:
        return str(value)


def _elapsed_seconds_since(start_value: str) -> int:
    """Return elapsed whole seconds from ISO-ish start value to now (>= 0)."""
    if not start_value:
        return 0
    try:
        start_dt = datetime.fromisoformat(str(start_value).replace('Z', '+00:00'))
        now_dt = datetime.now(start_dt.tzinfo) if start_dt.tzinfo else datetime.now()
        return max(0, int((now_dt - start_dt).total_seconds()))
    except Exception:
        return 0


def _bridge_allowed_hosts() -> set[str]:
    raw = str(os.getenv("KCM_BRIDGE_ALLOWED_HOSTS") or "").strip()
    hosts = {
        "127.0.0.1",
        "::1",
        "localhost",
    }
    if raw:
        hosts.update({item.strip().lower() for item in raw.split(",") if item.strip()})
    return hosts


def _bridge_request_is_local() -> bool:
    remote_addr = str(request.remote_addr or "").strip().lower()
    if not remote_addr:
        return False

    try:
        if ipaddress.ip_address(remote_addr).is_loopback:
            return True
    except ValueError:
        pass

    return remote_addr in _bridge_allowed_hosts()


def _bridge_auth_error(message: str = "Unauthorized"):
    return jsonify({"error": message}), 401


def _bridge_forbidden_error(message: str = "Bridge endpoints accept local requests only."):
    return jsonify({"error": message}), 403


def _bridge_token_is_valid() -> bool:
    expected = str(os.getenv("KCM_BRIDGE_TOKEN") or "").strip()
    if not expected:
        return True  # No token configured → allow all local requests
    auth = str(request.headers.get("Authorization") or "").strip()
    token = auth.removeprefix("Bearer ").strip() if auth.lower().startswith("bearer ") else ""
    return token == expected


def _bridge_student_classification(student_row) -> str:
    if not student_row:
        return ""

    def _as_int(index: int) -> int:
        try:
            return int(bool(student_row[index])) if len(student_row) > index else 0
        except Exception:
            return 0

    if _as_int(10):
        return "assisted"
    if _as_int(11):
        return "monitored"
    if _as_int(22):
        return "independent"
    if _as_int(12):
        return "virtual"
    return ""


def _bridge_student_payload(student_row) -> dict:
    raw_subjects = str(student_row[17] or "") if len(student_row) > 17 else ""
    parsed_subjects = []
    if raw_subjects:
        try:
            maybe_subjects = json.loads(raw_subjects)
            if isinstance(maybe_subjects, list):
                parsed_subjects = [str(item).strip() for item in maybe_subjects if str(item or "").strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_subjects = []

    guardian_name = str(student_row[5] or "").strip() if len(student_row) > 5 else ""
    email = str(student_row[3] or "").strip() if len(student_row) > 3 else ""
    active_value = 1 if len(student_row) > 7 and bool(student_row[7]) else 0

    return {
        "id": int(student_row[0] or 0) if student_row else 0,
        "name": str(student_row[1] or "").strip() if len(student_row) > 1 else "",
        "email": email,
        "student_email": email,
        "phone": str(student_row[4] or "").strip() if len(student_row) > 4 else "",
        "guardian_name": guardian_name,
        "guardian": guardian_name,
        "active": active_value,
        "subject": str(student_row[2] or "").strip() if len(student_row) > 2 else "",
        "subjects_json": raw_subjects or "[]",
        "subjects": parsed_subjects or ([str(student_row[2]).strip()] if len(student_row) > 2 and str(student_row[2] or "").strip() else []),
        "classification": _bridge_student_classification(student_row),
        "el": 1 if len(student_row) > 10 and bool(student_row[10]) else 0,
        "pi": 1 if len(student_row) > 11 and bool(student_row[11]) else 0,
        "v": 1 if len(student_row) > 12 and bool(student_row[12]) else 0,
    }


def _resolve_student_id_from_scan_payload(payload: dict) -> int | None:
    """Resolve student id from scan payload using UID first, then id/name fallbacks."""
    payload = payload or {}

    # Prefer UID from regenerated QR payloads.
    # Scanner inputs can occasionally mangle repeated digits in ID-only scans
    # (e.g., 111 becoming 11), while UID remains globally unique and stable.
    uid = str(payload.get("student_uid") or "").strip()
    if uid:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            try:
                row = c.execute(
                    """
                    SELECT owner_id
                    FROM qr_token_registry
                    WHERE LOWER(COALESCE(token, '')) = LOWER(?)
                      AND LOWER(COALESCE(owner_type, '')) = 'student'
                    LIMIT 1
                    """,
                    (uid,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row and row[0]:
                try:
                    resolved = int(row[0])
                except (TypeError, ValueError):
                    resolved = 0
                if resolved > 0:
                    return resolved

    try:
        sid = int(payload.get("student_id") or 0)
    except (TypeError, ValueError):
        sid = 0

    if sid > 0:
        return sid

    name_hint = str(payload.get("student_name") or "").strip()
    if name_hint:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            row = c.execute(
                """
                SELECT id
                FROM students
                WHERE active = 1
                  AND LOWER(TRIM(COALESCE(name, ''))) = LOWER(TRIM(?))
                ORDER BY id DESC
                LIMIT 1
                """,
                (name_hint,),
            ).fetchone()
            if row and row[0]:
                try:
                    resolved = int(row[0])
                except (TypeError, ValueError):
                    resolved = 0
                if resolved > 0:
                    return resolved

    return None


def _bridge_send_email(data: dict) -> dict:
    email_manager = get_email_manager()
    sender_email = str(getattr(email_manager, "sender_email", "") or "").strip()
    sender_password = str(getattr(email_manager, "sender_password", "") or "").strip()
    smtp_server = str(getattr(email_manager, "smtp_server", "") or "").strip()
    smtp_port = int(getattr(email_manager, "smtp_port", 587) or 587)

    if not sender_email or not sender_password or not smtp_server:
        return {
            "success": False,
            "error": "SMTP configuration is not set. Please configure SMTP_SERVER, SMTP_PORT, SENDER_EMAIL, and SENDER_PASSWORD.",
        }

    to_email = str(data.get("to") or "").strip()
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    html_body = data.get("html_body")
    reply_to = str(data.get("reply_to") or "").strip()
    no_reply = bool(data.get("no_reply"))
    attachments_value = data.get("attachments")
    if attachments_value is None:
        attachments = []
    elif isinstance(attachments_value, list):
        attachments = attachments_value
    else:
        return {"success": False, "error": "attachments must be an array."}

    if not to_email or "@" not in to_email:
        return {"success": False, "error": "A valid recipient email address is required."}
    if not subject:
        return {"success": False, "error": "subject is required."}
    if not body:
        return {"success": False, "error": "body is required."}

    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject

    if no_reply:
        msg["Reply-To"] = reply_to or "noreply@kcm.local"
        msg["Precedence"] = "bulk"
        msg["List-Unsubscribe"] = "<mailto:noreply@kcm.local>"
    elif reply_to:
        msg["Reply-To"] = reply_to

    msg.set_content(body, subtype="plain", charset="utf-8")
    if isinstance(html_body, str) and html_body.strip():
        msg.add_alternative(html_body, subtype="html", charset="utf-8")

    for attachment in attachments:
        if not isinstance(attachment, dict):
            return {"success": False, "error": "Each attachment must be an object."}
        filename = str(attachment.get("filename") or "").strip()
        content_type = str(attachment.get("content_type") or "application/octet-stream").strip()
        content_b64 = str(attachment.get("content_base64") or "").strip()
        if not filename:
            return {"success": False, "error": "Attachment filename is required."}
        if not content_b64:
            return {"success": False, "error": f"Attachment '{filename}' is missing content_base64."}

        try:
            raw_bytes = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            return {"success": False, "error": f"Attachment '{filename}' has invalid base64 content."}

        main_type, sub_type = "application", "octet-stream"
        if "/" in content_type:
            main_type, sub_type = content_type.split("/", 1)
        else:
            guessed_type, _ = mimetypes.guess_type(filename)
            if guessed_type and "/" in guessed_type:
                main_type, sub_type = guessed_type.split("/", 1)

        msg.add_attachment(raw_bytes, maintype=main_type, subtype=sub_type, filename=filename)

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(sender_email, sender_password)
                server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        compact_password = sender_password.replace(" ", "")
        if compact_password and compact_password != sender_password:
            if smtp_port == 465:
                with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15) as server:
                    server.login(sender_email, compact_password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(sender_email, compact_password)
                    server.send_message(msg)
        else:
            return {
                "success": False,
                "error": "Authentication failed. Please check SENDER_EMAIL/SENDER_PASSWORD.",
            }
    except smtplib.SMTPException as exc:
        return {"success": False, "error": f"SMTP error: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Error sending email: {exc}"}

    return {"success": True, "message": f"Email sent successfully to {to_email}"}


def _send_checkout_email(student_row, start_time: str, end_time: str):
    """Send checkout notification email to the student's email on file (best effort).

    Mirrors the email_manager.send_email() pattern used by the utilities
    Student Activity Card send-email route.

    Returns a dict:
            - status: sent | disabled | no_email | failed | error
      - message: human-readable short message
    """
    import traceback as _tb

    try:
        if not student_row:
            return {"status": "error", "message": "Student not found for checkout email"}

        student_name = student_row[1] if len(student_row) > 1 else "Student"
        guardian_name = str(student_row[22] or '').strip() if len(student_row) > 22 else ''
        checkout_notify_enabled = bool(student_row[24]) if len(student_row) > 24 else True

        if not checkout_notify_enabled:
            print(f"[checkout-email] Skipped for {student_name}: checkout notifications disabled")
            return {"status": "disabled", "message": "Checkout notification disabled for this student"}

        recipient_email = (student_row[3] if len(student_row) > 3 else "") or ""
        recipient_email = recipient_email.strip()

        if not recipient_email or "@" not in recipient_email:
            print(f"[checkout-email] Skipped for {student_name}: no valid email on file")
            return {"status": "no_email", "message": "No email on file"}

        start_display = _format_checkout_timestamp(start_time)
        end_display = _format_checkout_timestamp(end_time)

        try:
            total_seconds = max(0, int(duration_seconds(start_time, end_time)))
        except Exception:
            total_seconds = 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        duration_display = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        profile = instructor_profile_manager.get_instructor_profile() or {}
        center_name = str(profile.get('center_location') or '').strip() or resolve_center_name()
        salutation = f"Dear {guardian_name}," if guardian_name else "Dear Parent/Guardian,"

        email_subject = f"{center_name} - Class Checkout - {student_name}"

        body = (
            f"{salutation}\n\n"
            f"{student_name} has checked out from class.\n\n"
            f"Guardian:       {guardian_name or 'Parent/Guardian'}\n"
            f"Start Time:       {start_display}\n"
            f"End Time:         {end_display}\n"
            f"Session Duration: {duration_display}\n\n"
            f"Center: {center_name}\n\n"
            f"This is an automated message. Please do not reply."
        )

        html_body = render_branded_email_shell(
            title=f"{center_name} Class Checkout Confirmation",
            center_name=center_name,
            subtitle=center_name,
            footer_note=f"This is an automated checkout message from {center_name}. Please do not reply to this email.",
            body_html=f"""
                <p>{salutation}</p>
                <div class="highlight"><strong>{student_name}</strong> has checked out from class.</div>
                <table class="report-table">
                    <tr><th>Guardian</th><td>{guardian_name or 'Parent/Guardian'}</td></tr>
                    <tr><th>Start Time</th><td>{start_display}</td></tr>
                    <tr><th>End Time</th><td>{end_display}</td></tr>
                    <tr><th>Session Duration</th><td>{duration_display}</td></tr>
                    <tr><th>Center</th><td>{center_name}</td></tr>
                </table>
            """
        )

        # Use the same email_manager pattern as utilities/report-card/send-email
        email_manager = get_email_manager()
        result = email_manager.send_email(
            recipient_email=recipient_email,
            subject=email_subject,
            body=body,
            html_body=html_body,
        )
        if result.get('success', False):
            print(f"[checkout-email] Sent to {recipient_email} for {student_name}")
            return {"status": "sent", "message": "Checkout email sent"}
        else:
            failure_reason = result.get('error') or 'Unknown email error'
            print(f"[checkout-email] Failed for {student_name}: {failure_reason}")
            return {"status": "failed", "message": f"Checkout email failed: {failure_reason}"}

    except Exception as e:
        print(f"[checkout-email] Unexpected error for student: {e}\n{_tb.format_exc()}")
        return {"status": "error", "message": f"Checkout email error: {e}"}


def register_api_routes(app):
    """Register API/AJAX routes."""

    @app.route("/api/bridge/students/list", methods=["GET"])
    def api_bridge_students_list():
        guard = _enforce_bridge_remote_access_policy()
        if guard is not None:
            return guard
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()
        students = student_manager.get_all_students()

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            active_rows = c.execute(
                """
                SELECT student_id, start_time
                FROM sessions
                WHERE end_time IS NULL
                """,
                (),
            ).fetchall()
            active_map = {sid: start for sid, start in active_rows}

            today = datetime.now().date().isoformat()
            today_rows = c.execute(
                """
                SELECT student_id, SUM(duration)
                FROM sessions
                WHERE DATE(start_time)=?
                  AND end_time IS NOT NULL
                GROUP BY student_id
                """,
                (today,),
            ).fetchall()

            today_sum = {sid: secs for sid, secs in today_rows}

            latest_rows = c.execute(
                """
                SELECT student_id, duration
                FROM sessions
                WHERE end_time IS NOT NULL
                ORDER BY id DESC
                """,
                (),
            ).fetchall()
            latest_duration = {}
            for sid, dur in latest_rows:
                if sid not in latest_duration:
                    latest_duration[sid] = dur

        result = []
        for s in students:
            sid = s[0]
            status = "registered"
            start_time = None
            total_seconds = None
            dur = latest_duration.get(sid)
            schedule_entries = _schedule_entries_from_student_row(s)

            if sid in active_map:
                status = "active"
                start_time = active_map[sid]
            elif sid in today_sum:
                status = "checked"
                total_seconds = today_sum.get(sid, 0)

            result.append({
                "id": sid,
                "name": s[1],
                "subject": s[2],
                "email": s[3],
                "phone": s[4],
                "guardian": s[5] if len(s) > 5 else '',
                "active": s[7] if len(s) > 7 else 0,
                "book_loaned": s[8] if len(s) > 8 else 0,
                "device_loaned": s[9] if len(s) > 9 else 0,
                "day1": schedule_entries[0]["day"] if len(schedule_entries) > 0 else None,
                "day1_time": schedule_entries[0]["time"] if len(schedule_entries) > 0 else None,
                "day2": schedule_entries[1]["day"] if len(schedule_entries) > 1 else None,
                "day2_time": schedule_entries[1]["time"] if len(schedule_entries) > 1 else None,
                "day3": schedule_entries[2]["day"] if len(schedule_entries) > 2 else None,
                "day3_time": schedule_entries[2]["time"] if len(schedule_entries) > 2 else None,
                "day4": schedule_entries[3]["day"] if len(schedule_entries) > 3 else None,
                "day4_time": schedule_entries[3]["time"] if len(schedule_entries) > 3 else None,
                "day5": schedule_entries[4]["day"] if len(schedule_entries) > 4 else None,
                "day5_time": schedule_entries[4]["time"] if len(schedule_entries) > 4 else None,
                "day6": schedule_entries[5]["day"] if len(schedule_entries) > 5 else None,
                "day6_time": schedule_entries[5]["time"] if len(schedule_entries) > 5 else None,
                "schedule": schedule_entries,
                "subjects": _subjects_from_student_row(s),
                "status": status,
                "start_time": start_time,
                "total_seconds": total_seconds,
                "duration": dur,
                "photo_url": f"/students/photo/{sid}" if _has_photo_blob(s) else "",
                "photo_data_uri": _photo_data_uri(s),
            })

        return jsonify(result), 200

    @app.route("/api/bridge/assistants/list", methods=["GET"])
    def api_bridge_assistants_list():
        guard = _enforce_bridge_remote_access_policy()
        if guard is not None:
            return guard
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        assistants = assistant_manager.get_all_assistants()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            _auto_close_stale_assistant_sessions(conn)
            open_rows = c.execute(
                "SELECT assistant_id, start_time FROM assistant_sessions WHERE end_time IS NULL",
                (),
            ).fetchall()

        open_map = {aid: start for (aid, start) in open_rows}
        payload = [
            dict(
                id=a[0],
                name=a[1],
                role=a[2] if len(a) > 2 else "",
                email=a[3] if len(a) > 3 else "",
                phone=a[4] if len(a) > 4 else "",
                loading=a[5] if len(a) > 5 else 1,
                on_duty=a[0] in open_map,
                start_time=open_map.get(a[0]),
            )
            for a in assistants
        ]
        return jsonify(payload), 200

    @app.route("/api/bridge/sessions/toggle", methods=["POST"])
    def api_bridge_sessions_toggle():
        guard = _enforce_bridge_remote_access_policy()
        if guard is not None:
            return guard
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        data = request.get_json(silent=True) or {}
        student_id = _resolve_student_id_from_scan_payload(data)
        if not student_id:
            return jsonify({"error": "Missing student_id"}), 400

        student = student_manager.get_student(student_id)
        if not student:
            return jsonify({"error": "Student not found"}), 404

        student_name = student[1]
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            open_session = c.execute(
                "SELECT id, start_time FROM sessions WHERE student_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
                (student_id,),
            ).fetchone()

        if open_session:
            open_session_id, open_start_time = open_session
            elapsed_seconds = _elapsed_seconds_since(open_start_time)
            if elapsed_seconds < CHECKOUT_COOLDOWN_SECONDS:
                wait_seconds = CHECKOUT_COOLDOWN_SECONDS - elapsed_seconds
                return jsonify({
                    "error": f"Please wait {wait_seconds} seconds before checkout.",
                    "action": "checkout_blocked",
                    "student_id": student_id,
                    "name": student_name,
                    "wait_seconds": wait_seconds,
                }), 429

            checkout_email_status = None
            checkout_email_message = None
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                open_row = c.execute(
                    """
                    SELECT id, start_time
                    FROM sessions
                    WHERE student_id = ?
                      AND end_time IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (student_id,),
                ).fetchone()
                if open_row:
                    sess_id, start = open_row
                    end = time_now()
                    try:
                        duration = duration_seconds(start, end)
                    except Exception:
                        duration = 0
                    c.execute(
                        "UPDATE sessions SET end_time = ?, duration = ?, sync_synced = 0 WHERE id = ?",
                        (end, duration, sess_id),
                    )
                    conn.commit()
                    _push_cloud_backup_after_student_change(student_id, "checkout", "api_bridge_sessions_toggle")
                    email_result = _send_checkout_email(student, start, end) or {}
                    checkout_email_status = email_result.get("status")
                    checkout_email_message = email_result.get("message")

            server_cache.invalidate(_students_list_cache_key())
            return jsonify({
                "action": "checked_out",
                "student_id": student_id,
                "name": student_name,
                "checkout_email_status": checkout_email_status,
                "checkout_email_message": checkout_email_message,
            }), 200

        timer_manager.start_session(student_id)
        _push_cloud_backup_after_student_change(student_id, "checkin", "api_bridge_sessions_toggle")
        server_cache.invalidate(_students_list_cache_key())
        return jsonify({
            "action": "started",
            "student_id": student_id,
            "name": student_name,
        }), 200

    @app.route("/api/bridge/sessions/active", methods=["GET"])
    def api_bridge_sessions_active():
        guard = _enforce_bridge_remote_access_policy()
        if guard is not None:
            return guard
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        today = datetime.now().date().isoformat()

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            active_rows = c.execute(
                """
                SELECT student_id, start_time
                FROM sessions
                WHERE end_time IS NULL
                  AND DATE(start_time)=?
                """,
                (today,),
            ).fetchall()

        students = {s[0]: s for s in student_manager.get_all_students()}
        result = []
        for sid, start in active_rows:
            s = students.get(sid)
            if not s:
                continue
            result.append({
                "id": sid,
                "name": s[1],
                "subject": s[2],
                "book_loaned": s[8] if len(s) > 8 else 0,
                "device_loaned": s[9] if len(s) > 9 else 0,
                "start_time": start,
                "subjects": _subjects_from_student_row(s),
                "total_study_minutes": _total_study_minutes_from_student_row(s),
                "photo_url": f"/students/photo/{sid}" if _has_photo_blob(s) else '',
                "photo_data_uri": _photo_data_uri(s),
            })

        return jsonify(result), 200

    @app.route("/api/bridge/reconcile/apply", methods=["POST"])
    def api_bridge_reconcile_apply():
        guard = _enforce_bridge_remote_access_policy()
        if guard is not None:
            return guard
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        body = request.get_json(silent=True) or {}
        op_id = str(body.get("op_id") or "").strip()
        op_type = str(body.get("op_type") or "").strip()
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}

        result = scanner_sync.apply_incoming_mutation(op_id=op_id, op_type=op_type, payload=payload)
        code = int(result.pop("code", 200))
        return jsonify(result), code

    try:
        from app import csrf
        csrf.exempt(api_bridge_reconcile_apply)
    except Exception:
        pass

    try:
        from app import csrf
        csrf.exempt(api_bridge_sessions_toggle)
    except Exception:
        pass

    @app.route("/api/students/export")
    def api_students_export():
        if not _bridge_request_is_local():
            return _bridge_forbidden_error()
        if not _bridge_token_is_valid():
            return _bridge_auth_error()

        try:
            students = [_bridge_student_payload(student_row) for student_row in student_manager.get_all_students()]
        except Exception as exc:
            return jsonify({"error": f"Database error: {exc}"}), 500

        return jsonify({"students": students, "count": len(students)}), 200

    @app.route("/api/email/send", methods=["POST"])
    def api_email_send():
        if not _bridge_request_is_local():
            return _bridge_forbidden_error()
        if not _bridge_token_is_valid():
            return _bridge_auth_error()

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "JSON request body is required."}), 400

        result = _bridge_send_email(data)
        if result.get("success"):
            status_code = 200
        else:
            error_text = str(result.get("error") or "")
            if error_text.startswith("SMTP error:"):
                status_code = 502
            elif error_text.startswith("SMTP configuration is not set"):
                status_code = 500
            elif error_text.startswith("Error sending email:"):
                status_code = 502
            else:
                status_code = 400
        return jsonify(result), status_code

    # This endpoint is called by a trusted local bridge using bearer-token auth,
    # so Flask-WTF CSRF protection would incorrectly reject machine-to-machine
    # requests that do not include browser form tokens.
    try:
        from app import csrf

        csrf.exempt(api_email_send)
    except Exception:
        # If CSRF is unavailable during an unusual import path, keep the route
        # registered; app-level startup already ensures CSRF is configured.
        pass
    
    @app.route("/api/students/list")
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_students_list():
        """Return students with computed status: registered | active | checked."""
        forwarded = _bridge_forward_get('/api/bridge/students/list')
        if forwarded is not None:
            return forwarded

        cache_key = _students_list_cache_key()

        def _build_students_list_payload():
            students = student_manager.get_all_students()

            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                active_rows = c.execute(
                    """
                    SELECT student_id, start_time
                    FROM sessions
                    WHERE end_time IS NULL
                    """,
                    (),
                ).fetchall()
                active_map = {sid: start for sid, start in active_rows}

                today = datetime.now().date().isoformat()
                today_rows = c.execute(
                    """
                    SELECT student_id, SUM(duration)
                    FROM sessions
                    WHERE DATE(start_time)=?
                      AND end_time IS NOT NULL
                    GROUP BY student_id
                    """,
                    (today,),
                ).fetchall()

                today_sum = {sid: secs for sid, secs in today_rows}

                latest_rows = c.execute(
                    """
                    SELECT student_id, duration
                    FROM sessions
                    WHERE end_time IS NOT NULL
                    ORDER BY id DESC
                    """,
                    (),
                ).fetchall()
                latest_duration = {}
                for sid, dur in latest_rows:
                    if sid not in latest_duration:
                        latest_duration[sid] = dur

            _trace_column3(
                "students_list_build_source",
                total_students=len(students),
                active_rows=len(active_rows),
                checked_rows=len(today_rows),
                latest_rows=len(latest_rows),
                today=today,
            )

            result = []
            checked_ids = []
            for s in students:
                sid = s[0]
                status = "registered"
                start_time = None
                total_seconds = None
                dur = latest_duration.get(sid)
                schedule_entries = _schedule_entries_from_student_row(s)

                if sid in active_map:
                    status = "active"
                    start_time = active_map[sid]
                elif sid in today_sum:
                    status = "checked"
                    total_seconds = today_sum.get(sid, 0)
                    checked_ids.append(sid)

                student_dict = {
                    "id": sid,
                    "name": s[1],
                    "subject": s[2],
                    "email": s[3],
                    "phone": s[4],
                    "guardian": s[5] if len(s) > 5 else '',
                    "active": s[7] if len(s) > 7 else 0,
                    "book_loaned": s[8] if len(s) > 8 else 0,
                    "device_loaned": s[9] if len(s) > 9 else 0,
                    "day1": schedule_entries[0]["day"] if len(schedule_entries) > 0 else None,
                    "day1_time": schedule_entries[0]["time"] if len(schedule_entries) > 0 else None,
                    "day2": schedule_entries[1]["day"] if len(schedule_entries) > 1 else None,
                    "day2_time": schedule_entries[1]["time"] if len(schedule_entries) > 1 else None,
                    "day3": schedule_entries[2]["day"] if len(schedule_entries) > 2 else None,
                    "day3_time": schedule_entries[2]["time"] if len(schedule_entries) > 2 else None,
                    "day4": schedule_entries[3]["day"] if len(schedule_entries) > 3 else None,
                    "day4_time": schedule_entries[3]["time"] if len(schedule_entries) > 3 else None,
                    "day5": schedule_entries[4]["day"] if len(schedule_entries) > 4 else None,
                    "day5_time": schedule_entries[4]["time"] if len(schedule_entries) > 4 else None,
                    "day6": schedule_entries[5]["day"] if len(schedule_entries) > 5 else None,
                    "day6_time": schedule_entries[5]["time"] if len(schedule_entries) > 5 else None,
                    "schedule": schedule_entries,
                    "subjects": _subjects_from_student_row(s),
                    "status": status,
                    "start_time": start_time,
                    "total_seconds": total_seconds,
                    "duration": dur,
                    "photo_url": f"/students/photo/{sid}" if _has_photo_blob(s) else "",
                    "photo_data_uri": _photo_data_uri(s),
                }
                result.append(student_dict)

            _trace_column3(
                "students_list_payload_built",
                cache_key=cache_key,
                checked_ids=checked_ids,
                checked_count=len(checked_ids),
            )
            return result

        result = server_cache.get_or_set(
            cache_key,
            _build_students_list_payload,
            policy="checkin_live",
        )

        checked_count = sum(1 for student in result if student.get("status") == "checked")
        _trace_column3(
            "students_list_response",
            cache_key=cache_key,
            total=len(result),
            checked_count=checked_count,
        )

        return jsonify(result)

    @app.route("/api/students/start/<int:sid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_students_start(sid):
        if _scanner_api_client_enabled():
            forwarded = _bridge_forward_post('/api/bridge/sessions/toggle', {"student_id": int(sid)})
            if forwarded is not None:
                return forwarded

        student = student_manager.get_student(sid)
        if not student:
            return jsonify({"error": "Student not found"}), 404
        timer_manager.start_session(sid)
        _push_cloud_backup_after_student_change(sid, "checkin", "api_students_start")
        server_cache.invalidate(_students_list_cache_key())
        if _scanner_api_client_enabled():
            scanner_sync.enqueue_mutation(
                "session_start",
                {
                    "student_id": int(sid),
                    "started_at": time_now(),
                },
            )
        return jsonify({"status": "started"})

    @app.route("/api/students/stop/<int:sid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_students_stop(sid):
        if _scanner_api_client_enabled():
            forwarded = _bridge_forward_post('/api/bridge/sessions/toggle', {"student_id": int(sid)})
            if forwarded is not None:
                return forwarded

        student = student_manager.get_student(sid)
        if not student:
            return jsonify({"error": "Student not found"}), 404
        _trace_column3("checkout_begin", sid=sid, student_name=student[1])
        checkout_email_status = None
        checkout_email_message = None
        end = None
        duration = None
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            open_row = c.execute(
                """
                SELECT id, start_time
                FROM sessions
                WHERE student_id = ?
                  AND end_time IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (sid,),
            ).fetchone()
            if open_row:
                sess_id, start = open_row
                end = time_now()
                try:
                    duration = duration_seconds(start, end)
                except Exception:
                    duration = 0
                c.execute(
                    "UPDATE sessions SET end_time = ?, duration = ?, sync_synced = 0 WHERE id = ?",
                    (end, duration, sess_id),
                )
                conn.commit()
                _push_cloud_backup_after_student_change(sid, "checkout", "api_students_stop")
                _trace_column3(
                    "checkout_db_updated",
                    sid=sid,
                    sess_id=sess_id,
                    duration=duration,
                    start=start,
                    end=end,
                )
                email_result = _send_checkout_email(student, start, end) or {}
                checkout_email_status = email_result.get("status")
                checkout_email_message = email_result.get("message")
        cache_key = _students_list_cache_key()
        server_cache.invalidate(cache_key)
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            today = datetime.now().date().isoformat()
            checked_total = c.execute(
                """
                SELECT COUNT(DISTINCT student_id)
                FROM sessions
                WHERE DATE(start_time)=?
                  AND end_time IS NOT NULL
                """,
                (today,),
            ).fetchone()[0] or 0
        _trace_column3(
            "checkout_complete",
            sid=sid,
            cache_key=cache_key,
            checkout_email_status=checkout_email_status,
            checked_total=checked_total,
        )
        if _scanner_api_client_enabled():
            scanner_sync.enqueue_mutation(
                "session_stop",
                {
                    "student_id": int(sid),
                    "end_time": end or time_now(),
                    "duration": int(duration or 0),
                },
            )
        return jsonify({
            "status": "stopped",
            "checkout_email_status": checkout_email_status,
            "checkout_email_message": checkout_email_message,
        })

    @app.route("/api/sessions/active")
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_sessions_active():
        """Return only currently active sessions; auto-stop any over 2h."""
        forwarded = _bridge_forward_get('/api/bridge/sessions/active')
        if forwarded is not None:
            return forwarded

        now_str = time_now()
        today = datetime.now().date().isoformat()

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            active_rows = c.execute(
                """
                SELECT student_id, start_time
                FROM sessions
                WHERE end_time IS NULL
                  AND DATE(start_time)=?
                """,
                (today,),
            ).fetchall()

        for sid, start in list(active_rows):
            try:
                if duration_seconds(start, now_str) >= 7200:
                    with sqlite3.connect(DB_PATH) as conn:
                        c = conn.cursor()
                        end = time_now()
                        try:
                            duration = duration_seconds(start, end)
                        except Exception:
                            duration = 0
                        c.execute(
                            """
                            UPDATE sessions
                            SET end_time = ?, duration = ?, sync_synced = 0
                            WHERE id = (
                                SELECT id
                                FROM sessions
                                WHERE student_id = ?
                                  AND end_time IS NULL
                                ORDER BY id DESC
                                LIMIT 1
                            )
                            """,
                            (end, duration, sid),
                        )
                        conn.commit()
                    _trace_column3(
                        "active_session_auto_closed",
                        sid=sid,
                        duration=duration,
                        start=start,
                        end=end,
                    )
            except Exception:
                continue

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            active_rows = c.execute(
                """
                SELECT student_id, start_time
                FROM sessions
                WHERE end_time IS NULL
                  AND DATE(start_time)=?
                """,
                (today,),
            ).fetchall()

        students = {s[0]: s for s in student_manager.get_all_students()}
        result = []
        for sid, start in active_rows:
            s = students.get(sid)
            if not s:
                continue
            result.append({
                "id": sid,
                "name": s[1],
                "subject": s[2],
                "book_loaned": s[8] if len(s) > 8 else 0,
                "device_loaned": s[9] if len(s) > 9 else 0,
                "start_time": start,
                "subjects": _subjects_from_student_row(s),
                "total_study_minutes": _total_study_minutes_from_student_row(s),
                "photo_url": f"/students/photo/{sid}" if _has_photo_blob(s) else '',
                "photo_data_uri": _photo_data_uri(s),
            })

        return jsonify(result)

    @app.route("/api/sessions/clear", methods=["POST"])
    @require_admin
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_sessions_clear():
        """Stop all active sessions (DB + cache) and clear timer buffers."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("DELETE FROM sessions")
                closed_rows = c.rowcount
                conn.commit()
            ended = []
            server_cache.invalidate(_students_list_cache_key())
            return jsonify({"stopped": ended, "closed_rows": closed_rows}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/sessions/toggle", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_sessions_toggle():
        """Toggle a student's session: start if not active, stop if active.
        Request JSON: {"student_id": <id>}
        Returns: {"action": "started"|"checked_out", "student_id": <id>, "name": <name>}
        """
        forwarded = _bridge_forward_post('/api/bridge/sessions/toggle', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        try:
            data = request.get_json() or {}
            student_id = _resolve_student_id_from_scan_payload(data)
            
            if not student_id:
                return jsonify({"error": "Missing student_id"}), 400
            
            # Get student info
            student = student_manager.get_student(student_id)
            if not student:
                return jsonify({"error": "Student not found"}), 404
            
            student_name = student[1]  # name is at index 1
            
            # Check if student has an open session
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                open_session = c.execute(
                    "SELECT id, start_time FROM sessions WHERE student_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
                    (student_id,)
                ).fetchone()
            
            if open_session:
                open_session_id, open_start_time = open_session
                elapsed_seconds = _elapsed_seconds_since(open_start_time)
                if elapsed_seconds < CHECKOUT_COOLDOWN_SECONDS:
                    wait_seconds = CHECKOUT_COOLDOWN_SECONDS - elapsed_seconds
                    return jsonify({
                        "error": f"Please wait {wait_seconds} seconds before checkout.",
                        "action": "checkout_blocked",
                        "student_id": student_id,
                        "name": student_name,
                        "wait_seconds": wait_seconds,
                    }), 429

                # Stop the session (check out)
                checkout_email_status = None
                checkout_email_message = None
                end = time_now()
                duration = 0
                with sqlite3.connect(DB_PATH) as conn:
                    c = conn.cursor()
                    open_row = c.execute(
                        """
                        SELECT id, start_time
                        FROM sessions
                        WHERE student_id = ?
                          AND end_time IS NULL
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (student_id,),
                    ).fetchone()
                    if open_row:
                        sess_id, start = open_row
                        end = time_now()
                        try:
                            duration = duration_seconds(start, end)
                        except Exception:
                            duration = 0
                        c.execute(
                            "UPDATE sessions SET end_time = ?, duration = ?, sync_synced = 0 WHERE id = ?",
                            (end, duration, sess_id),
                        )
                        conn.commit()
                        _push_cloud_backup_after_student_change(student_id, "checkout", "api_sessions_toggle")
                        _trace_column3(
                            "toggle_checkout_db_updated",
                            student_id=student_id,
                            sess_id=sess_id,
                            duration=duration,
                            start=start,
                            end=end,
                        )
                        email_result = _send_checkout_email(student, start, end) or {}
                        checkout_email_status = email_result.get("status")
                        checkout_email_message = email_result.get("message")
                cache_key = _students_list_cache_key()
                server_cache.invalidate(cache_key)
                _trace_column3(
                    "toggle_checkout_complete",
                    student_id=student_id,
                    cache_key=cache_key,
                    checkout_email_status=checkout_email_status,
                )
                if _scanner_api_client_enabled() and bool(getattr(g, "scanner_bridge_fallback", False)):
                    scanner_sync.enqueue_mutation(
                        "session_stop",
                        {
                            "student_id": int(student_id),
                            "end_time": end,
                            "duration": int(duration or 0),
                        },
                    )
                return jsonify({
                    "action": "checked_out",
                    "student_id": student_id,
                    "name": student_name,
                    "checkout_email_status": checkout_email_status,
                    "checkout_email_message": checkout_email_message,
                }), 200
            else:
                # Start a new session
                timer_manager.start_session(student_id)
                _push_cloud_backup_after_student_change(student_id, "checkin", "api_sessions_toggle")
                server_cache.invalidate(_students_list_cache_key())
                if _scanner_api_client_enabled() and bool(getattr(g, "scanner_bridge_fallback", False)):
                    scanner_sync.enqueue_mutation(
                        "session_start",
                        {
                            "student_id": int(student_id),
                            "started_at": time_now(),
                        },
                    )
                return jsonify({
                    "action": "started",
                    "student_id": student_id,
                    "name": student_name
                }), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/attendance/reset_today", methods=["POST"])
    @require_admin
    def api_attendance_reset_today():
        """Reset today's attendance data and clear any active class timers.
        - Stops all active sessions
        - Deletes sessions whose start_time is today
        - Clears active cache for dashboard columns
        """
        # Stop any active timers first
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            open_rows = c.execute(
                "SELECT id, student_id, start_time FROM sessions WHERE end_time IS NULL",
                ()
            ).fetchall()
            end = time_now()
            for sess_id, sid, start in open_rows:
                try:
                    duration = duration_seconds(start, end)
                except Exception:
                    duration = 0
                c.execute(
                    "UPDATE sessions SET end_time = ?, duration = ?, sync_synced = 0 WHERE id = ?",
                    (end, duration, sess_id),
                )
            conn.commit()

        today = datetime.now().date().isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM sessions WHERE DATE(start_time)=?", (today))
            deleted = c.rowcount
            conn.commit()

        server_cache.invalidate(_students_list_cache_key())
        return jsonify({"deleted": deleted, "date": today})

    @app.route("/api/assistants/profiles")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_profiles():
        """Return assistant static profile list with longer TTL lane."""
        def _build_profiles_payload():
            rows = assistant_manager.get_all_assistants()
            return [
                dict(
                    id=a[0],
                    name=a[1],
                    role=a[2] if len(a) > 2 else "",
                    email=a[3] if len(a) > 3 else "",
                    phone=a[4] if len(a) > 4 else "",
                    loading=a[5] if len(a) > 5 else 1,
                )
                for a in rows
            ]

        payload = server_cache.get_or_set(
            _assistants_profile_cache_key(),
            _build_profiles_payload,
            policy="assistant_profile",
        )
        return jsonify(payload)

    @app.route("/api/assistants/list")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_list():
        """Return all assistants with on-duty status and start time.
        DB is the source of truth: an "open" assistant_sessions row (end_time NULL) => on duty.
        """
        forwarded = _bridge_forward_get('/api/bridge/assistants/list')
        if forwarded is not None:
            return forwarded

        _trace_staff_duty("list_request", method=request.method, path=request.path)

        def _build_duty_payload():
            _trace_staff_duty("list_build_begin")
            assistants = assistant_manager.get_all_assistants()
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                auto_closed = _auto_close_stale_assistant_sessions(conn)
                if auto_closed:
                    _trace_staff_duty("list_auto_closed_stale", count=auto_closed)
                try:
                    open_rows = c.execute(
                        "SELECT assistant_id, start_time FROM assistant_sessions WHERE end_time IS NULL",
                        (),
                    ).fetchall()
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "no such table" in msg and "assistant_sessions" in msg:
                        _trace_staff_duty("list_missing_table_autocreate")
                        c.execute(
                            """
                            CREATE TABLE IF NOT EXISTS assistant_sessions (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                assistant_id INTEGER,
                                start_time TEXT,
                                end_time TEXT,
                                duration INTEGER,
                                FOREIGN KEY(assistant_id) REFERENCES staff(id)
                            )
                            """,
                            (),
                        )
                        conn.commit()
                        open_rows = []
                    else:
                        raise
            _trace_staff_duty(
                "list_build_source",
                assistants_count=len(assistants),
                open_rows_count=len(open_rows),
            )
            open_map = {aid: start for (aid, start) in open_rows}
            result = []
            for a in assistants:
                aid = a[0]
                result.append(
                    dict(
                        id=aid,
                        name=a[1],
                        role=a[2] if len(a) > 2 else "",
                        email=a[3] if len(a) > 3 else "",
                        phone=a[4] if len(a) > 4 else "",
                        loading=a[5] if len(a) > 5 else 1,
                        on_duty=aid in open_map,
                        start_time=open_map.get(aid),
                    )
                )
            _trace_staff_duty("list_build_done", payload_count=len(result))
            return result

        try:
            # Duty state changes frequently (including mailbox imports); return live DB view.
            payload = _build_duty_payload()
            _trace_staff_duty("list_response_ok", payload_type=type(payload).__name__, payload_count=(len(payload) if isinstance(payload, list) else -1))
            return jsonify(payload)
        except Exception as e:
            _trace_staff_duty("list_response_error", error=str(e))
            print(traceback.format_exc())
            return jsonify({"error": f"Staff list failed: {e}"}), 500

    @app.route("/api/assistants/select/<int:aid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_select(aid):
        """Toggle assistant on/off duty with payroll time tracking.
        Uses DB open-row semantics so checkout works reliably (even after restarts).
        """
        _trace_staff_duty("select_request", aid=aid, method=request.method, path=request.path)
        try:
            assistant = assistant_manager.get_assistant(aid)
            if not assistant:
                _trace_staff_duty("select_not_found", aid=aid)
                return jsonify({"error": "Staff member not found"}), 404

            now = datetime.now()
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                _auto_close_stale_assistant_sessions(conn)
                open_row = cur.execute(
                    "SELECT id, start_time FROM assistant_sessions WHERE assistant_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
                    (aid,),
                ).fetchone()

                _trace_staff_duty("select_open_row", aid=aid, has_open_row=bool(open_row))

                if open_row:
                    sess_id, start_iso = open_row
                    try:
                        start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                    except Exception:
                        start_dt = None

                    day_iso = now.date().isoformat()
                    closed_today = _assistant_closed_seconds_today(cur, int(aid), day_iso)
                    remaining = max(0, STAFF_DUTY_MAX_DAILY_SECONDS - closed_today)
                    raw_duration = int((now - start_dt).total_seconds()) if start_dt else 0
                    duration = min(max(0, raw_duration), remaining)
                    end_dt = (start_dt + timedelta(seconds=duration)) if start_dt else now
                    cur.execute(
                        "UPDATE assistant_sessions SET end_time=?, duration=?, sync_synced = 0 WHERE id=?",
                        (end_dt.isoformat(), duration, sess_id),
                    )
                    conn.commit()
                    _push_cloud_backup_after_staff_change(aid, "checkout")
                    server_cache.invalidate(_assistants_duty_cache_key())
                    if _scanner_api_client_enabled():
                        scanner_sync.enqueue_mutation(
                            "assistant_set_duty",
                            {
                                "assistant_id": int(aid),
                                "on_duty": False,
                                "changed_at": datetime.now().isoformat(),
                            },
                        )
                    _trace_staff_duty("select_checkout_ok", aid=aid, sess_id=sess_id, duration=duration)
                    return jsonify({"success": True, "on_duty": False, "duration": duration})
                else:
                    day_iso = now.date().isoformat()
                    used_today = _assistant_closed_seconds_today(cur, int(aid), day_iso)
                    if used_today >= STAFF_DUTY_MAX_DAILY_SECONDS:
                        _trace_staff_duty("select_checkin_blocked_daily_limit", aid=aid, used_today=used_today)
                        return jsonify({
                            "success": False,
                            "error": "Daily on-duty limit reached (6 hours).",
                            "on_duty": False,
                            "daily_limit_seconds": STAFF_DUTY_MAX_DAILY_SECONDS,
                            "used_today_seconds": used_today,
                        }), 409

                    # Start new open session
                    cur.execute(
                        "INSERT INTO assistant_sessions (assistant_id, start_time, end_time, duration) VALUES (?, ?, NULL, NULL)",
                        (aid, now.isoformat()),
                    )
                    conn.commit()
                    _push_cloud_backup_after_staff_change(aid, "checkin")
                    server_cache.invalidate(_assistants_duty_cache_key())
                    if _scanner_api_client_enabled():
                        scanner_sync.enqueue_mutation(
                            "assistant_set_duty",
                            {
                                "assistant_id": int(aid),
                                "on_duty": True,
                                "changed_at": datetime.now().isoformat(),
                            },
                        )
                    _trace_staff_duty("select_checkin_ok", aid=aid)
                    return jsonify({"success": True, "on_duty": True})
        except Exception as e:
            _trace_staff_duty("select_error", aid=aid, error=str(e))
            print(traceback.format_exc())
            return jsonify({"error": f"Staff toggle failed: {e}"}), 500

    @app.route("/api/assistants/reset-duty", methods=["POST"])
    @require_admin
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_reset_duty():
        """Force-clear all open staff duty rows (reset stale on-duty flags)."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                closed = _force_reset_all_open_assistant_sessions(conn)
            _push_cloud_backup_after_staff_change(-1, "reset_all")
            server_cache.invalidate(_assistants_duty_cache_key())
            _trace_staff_duty("reset_duty_done", closed=closed)
            return jsonify({"success": True, "closed": closed}), 200
        except Exception as exc:
            _trace_staff_duty("reset_duty_error", error=str(exc))
            return jsonify({"success": False, "error": str(exc)}), 500
