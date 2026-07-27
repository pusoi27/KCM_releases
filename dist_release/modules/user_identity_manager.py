import re
import sqlite3
import hashlib
import os
import threading
from datetime import datetime

from modules.database import DB_PATH
from modules import instructor_profile_manager


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OWNER_BINDING_VERSION = "email-v1"
_OWNER_BINDING_VERSION_KEY = "owner_binding_version"
_OWNER_BINDING_HASH_KEY = "owner_binding_hash"
_OWNER_BINDING_EMAIL_KEY = "owner_binding_email"
_OWNER_BINDING_UPDATED_AT_KEY = "owner_binding_updated_at"
_OWNER_SIGNATURE_LOCK = threading.RLock()


def is_valid_email(email: str) -> bool:
    if not email:
        return False
    return bool(EMAIL_RE.match(email.strip()))


def _ensure_identity_table():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS app_identity (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                email TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def _ensure_metadata_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _normalized_email(email: str) -> str:
    return str(email or "").strip().lower()


def _email_owner_signature(email: str) -> str:
    normalized = _normalized_email(email)
    payload = f"stdytime-owner::{_OWNER_BINDING_VERSION}::{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _get_metadata_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(
        "SELECT COALESCE(meta_value, '') FROM app_metadata WHERE meta_key = ? LIMIT 1",
        (str(key or "").strip(),),
    ).fetchone()
    return str((row[0] if row else "") or "").strip()


def _set_metadata_value(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        """
        INSERT INTO app_metadata (meta_key, meta_value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(meta_key) DO UPDATE SET
            meta_value = excluded.meta_value,
            updated_at = excluded.updated_at
        """,
        (str(key or "").strip(), str(value or "").strip(), datetime.now().isoformat()),
    )


def _bind_owner_signature(conn: sqlite3.Connection, email: str) -> None:
    _ensure_metadata_table(conn)
    normalized = _normalized_email(email)
    _set_metadata_value(conn, _OWNER_BINDING_VERSION_KEY, _OWNER_BINDING_VERSION)
    _set_metadata_value(conn, _OWNER_BINDING_HASH_KEY, _email_owner_signature(normalized))
    _set_metadata_value(conn, _OWNER_BINDING_EMAIL_KEY, normalized)
    _set_metadata_value(conn, _OWNER_BINDING_UPDATED_AT_KEY, datetime.now().isoformat())


def get_email_owner_binding_status(email: str) -> dict:
    """Return ownership-binding preflight status for a candidate instructor email.

    This function is read-only and performs no DB mutation.
    """
    normalized = _normalized_email(email)
    if not is_valid_email(normalized):
        return {
            "ok": False,
            "reason": "invalid_email",
            "message": "Please enter a valid instructor email.",
            "will_reinitialize": False,
            "has_binding": False,
            "bound_email": "",
        }

    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure_metadata_table(conn)
            stored_hash = _get_metadata_value(conn, _OWNER_BINDING_HASH_KEY)
            bound_email = _get_metadata_value(conn, _OWNER_BINDING_EMAIL_KEY)

            if not stored_hash:
                return {
                    "ok": True,
                    "reason": "legacy_unbound",
                    "message": "Database has no ownership signature yet and will be bound to this email.",
                    "will_reinitialize": False,
                    "has_binding": False,
                    "bound_email": "",
                }

            expected_hash = _email_owner_signature(normalized)
            if expected_hash == stored_hash:
                return {
                    "ok": True,
                    "reason": "match",
                    "message": "Ownership signature matches this instructor email.",
                    "will_reinitialize": False,
                    "has_binding": True,
                    "bound_email": bound_email,
                }

            return {
                "ok": True,
                "reason": "mismatch",
                "message": "Ownership signature belongs to a different instructor email.",
                "will_reinitialize": True,
                "has_binding": True,
                "bound_email": bound_email,
            }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "read_failed",
            "message": f"Unable to verify database ownership signature: {exc}",
            "will_reinitialize": False,
            "has_binding": False,
            "bound_email": "",
        }


def _quarantine_db_for_owner_mismatch() -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = f"{DB_PATH}.owner_mismatch_{timestamp}.bak"
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    os.replace(DB_PATH, archive_path)

    # Best effort cleanup of SQLite sidecar files for the replaced DB.
    for suffix in ("-wal", "-shm"):
        try:
            sidecar = f"{DB_PATH}{suffix}"
            if os.path.exists(sidecar):
                os.remove(sidecar)
        except Exception:
            pass

    return archive_path


def enforce_email_owner_signature(email: str) -> dict:
    """Ensure the active DB is bound to a single instructor email identity.

    Legacy DBs without binding metadata are auto-bound on first successful email.
    If a mismatch is detected, the current DB is quarantined and a fresh DB is initialized.
    """
    normalized = _normalized_email(email)
    if not is_valid_email(normalized):
        return {
            "ok": False,
            "action": "invalid_email",
            "message": "Instructor email is required to bind this database.",
        }

    with _OWNER_SIGNATURE_LOCK:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                _ensure_metadata_table(conn)
                stored_hash = _get_metadata_value(conn, _OWNER_BINDING_HASH_KEY)

                # Legacy DB bootstrap: bind on first valid email.
                if not stored_hash:
                    _bind_owner_signature(conn, normalized)
                    _ensure_identity_table()
                    conn.execute(
                        """
                        INSERT INTO app_identity (id, email, updated_at)
                        VALUES (1, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            email = excluded.email,
                            updated_at = excluded.updated_at
                        """,
                        (normalized, datetime.now().isoformat()),
                    )
                    conn.commit()
                    return {
                        "ok": True,
                        "action": "bound_legacy",
                        "message": "Database ownership signature initialized.",
                    }

                expected_hash = _email_owner_signature(normalized)
                if stored_hash == expected_hash:
                    _bind_owner_signature(conn, normalized)
                    conn.commit()
                    return {
                        "ok": True,
                        "action": "matched",
                        "message": "Database ownership signature matched.",
                    }
        except Exception as exc:
            return {
                "ok": False,
                "action": "read_failed",
                "message": f"Unable to verify database ownership signature: {exc}",
            }

        # Signature mismatch: quarantine current DB and initialize a fresh one.
        try:
            archived_path = _quarantine_db_for_owner_mismatch()
        except Exception as exc:
            return {
                "ok": False,
                "action": "quarantine_failed",
                "message": f"Database ownership mismatch detected, but archive step failed: {exc}",
            }

        try:
            # Local import avoids module import cycles at file-load time.
            from modules import database as database_module

            database_module.init_db()
            with sqlite3.connect(DB_PATH) as conn:
                _bind_owner_signature(conn, normalized)
                _ensure_identity_table()
                conn.execute(
                    """
                    INSERT INTO app_identity (id, email, updated_at)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        email = excluded.email,
                        updated_at = excluded.updated_at
                    """,
                    (normalized, datetime.now().isoformat()),
                )
                conn.commit()
            return {
                "ok": True,
                "action": "reinitialized",
                "archived_db_path": archived_path,
                "message": "Database was reinitialized for a different instructor email.",
            }
        except Exception as exc:
            return {
                "ok": False,
                "action": "reinitialize_failed",
                "message": (
                    "Database ownership mismatch detected, but fresh initialization failed: "
                    f"{exc}. Archived DB: {archived_path}"
                ),
            }


def get_saved_email() -> str | None:
    _ensure_identity_table()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        row = c.execute("SELECT email FROM app_identity WHERE id = 1", ()).fetchone()
        if not row:
            return None
        email = (row[0] or "").strip()
        return email if is_valid_email(email) else None


def save_email(email: str):
    email = (email or "").strip()
    if not is_valid_email(email):
        return

    _ensure_identity_table()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO app_identity (id, email, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                updated_at = excluded.updated_at
            """,
            (email, datetime.now().isoformat()),
        )
        conn.commit()


def resolve_active_email(session_email: str | None = None) -> str | None:
    if session_email and is_valid_email(session_email):
        return session_email.strip()

    saved = get_saved_email()
    if saved:
        return saved

    try:
        profile = instructor_profile_manager.get_instructor_profile()
    except Exception:
        profile = None

    profile_email = ((profile or {}).get("email") or "").strip()
    if is_valid_email(profile_email):
        save_email(profile_email)
        return profile_email

    return None


def clear_saved_email():
    """Remove the persisted email from app_identity (forces re-entry on next request)."""
    _ensure_identity_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM app_identity WHERE id = 1")
        conn.commit()


def sync_instructor_profile_email(email: str):
    email = (email or "").strip()
    if not is_valid_email(email):
        return

    try:
        profile = instructor_profile_manager.get_instructor_profile()
    except Exception:
        profile = None

    if not profile or not profile.get("id"):
        return

    current_email = ((profile.get("email") or "").strip())
    if current_email.lower() == email.lower():
        return

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE instructor_profile SET email = ?, updated_at = ? WHERE id = ?",
            (email, datetime.now().isoformat(), profile.get("id")),
        )
        conn.commit()
