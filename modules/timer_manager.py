# modules/timer_manager.py
import sqlite3
from modules.database import DB_PATH
from modules.utils import time_now, duration_seconds


def close_all_open_db_sessions():
    """Close ALL DB sessions with end_time IS NULL.
    This is stronger than stop_session(sid) because it closes multiple
    open rows per student if any exist. Returns count of rows updated.
    """
    end = time_now()
    updated = 0
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id, start_time FROM sessions WHERE end_time IS NULL"
        ).fetchall()
        for sid, start in rows:
            try:
                dur = duration_seconds(start, end)
            except Exception:
                # if parsing fails, set 0 duration
                dur = 0
            c.execute(
                "UPDATE sessions SET end_time=?, duration=? WHERE id=?",
                (end, dur, sid),
            )
            updated += 1
        conn.commit()
    return updated

def delete_all_open_db_sessions():
    """Hard delete all sessions with end_time IS NULL.
    Use when a clean slate is required (e.g., app restart/reset).
    Returns number of rows deleted.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM sessions WHERE end_time IS NULL")
        deleted = c.rowcount
        conn.commit()
    return deleted

def delete_all_sessions():
    """Delete ALL session records (open or closed) and clear caches.
    Use when a full reset of the active class/state is required.
    Returns number of rows deleted.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM sessions")
        deleted = c.rowcount
        conn.commit()
    return deleted

def start_session(student_id):
    """Insert a new open session row into the DB."""
    start = time_now()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO sessions (student_id, start_time) VALUES (?, ?, ?)""",
            (student_id, start)
        )
        conn.commit()
