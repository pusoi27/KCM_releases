import re
import sqlite3
from datetime import datetime

from modules.database import DB_PATH
from modules import instructor_profile_manager


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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
