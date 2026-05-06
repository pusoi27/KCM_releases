#*****************************
# schedule_manager.py - Assistant scheduling by day
# Version: 1.0.0
#*****************************
"""
CRUD operations for assistant scheduling (assigns assistants to specific calendar dates).
"""

import sqlite3
from datetime import datetime, timedelta
from modules.database import DB_PATH


def schedule_assistant(assistant_id, scheduled_date):
    """
    Schedule an assistant for a specific date (YYYY-MM-DD format).
    Returns True if inserted, False if already scheduled.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        try:
            c.execute(
                """INSERT INTO assistant_schedule (assistant_id, scheduled_date)
                   VALUES (?, ?, ?)""",
                (assistant_id, scheduled_date),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Already scheduled or other constraint violation
            return False


def unschedule_assistant(assistant_id, scheduled_date):
    """
    Remove an assistant from a scheduled date.
    Returns number of rows deleted.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """DELETE FROM assistant_schedule 
               WHERE assistant_id = ? AND scheduled_date = ?""",
            (assistant_id, scheduled_date),
        )
        conn.commit()
        return c.rowcount


def get_scheduled_assistants_for_date(scheduled_date):
    """
    Fetch all assistants scheduled for a specific date.
    Returns list of (assistant_id, name, role, email, phone).
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT s.id, s.name, s.role, s.email, s.phone
               FROM staff s
               INNER JOIN assistant_schedule a
                 ON s.id = a.assistant_id
               WHERE a.scheduled_date = ?
               ORDER BY s.name""",
            (scheduled_date),
        )
        return c.fetchall()


def is_assistant_scheduled(assistant_id, scheduled_date):
    """Return True if the assistant is already scheduled for the given date and owner."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        row = c.execute(
            """SELECT 1
               FROM assistant_schedule
               WHERE assistant_id = ? AND scheduled_date = ?
               LIMIT 1""",
            (assistant_id, scheduled_date),
        ).fetchone()
        return row is not None


def get_unscheduled_assistants():
    """
    Fetch all assistants not currently in the schedule.
    Returns list of (assistant_id, name, role, email, phone).
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT id, name, role, email, phone
               FROM staff
               WHERE               ORDER BY name""",
            (),
        )
        return c.fetchall()


def get_assistants_schedule_for_month(year, month):
    """
    Fetch all scheduled assistants for a given month.
    Returns dict: {YYYY-MM-DD: [(assistant_id, name, role, email, phone), ...]}.
    """
    # Get first and last day of month
    first_day = datetime(year, month, 1).date()
    if month == 12:
        last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)
    
    start_str = first_day.isoformat()
    end_str = last_day.isoformat()
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT a.scheduled_date, s.id, s.name, s.role, s.email, s.phone
               FROM assistant_schedule a
               INNER JOIN staff s ON s.id = a.assistant_id
               WHERE a.scheduled_date BETWEEN ? AND ?
               ORDER BY a.scheduled_date, s.name""",
            (start_str, end_str),
        )
        
        result = {}
        for row in c.fetchall():
            date_str = row[0]
            assistant = row[1:]
            if date_str not in result:
                result[date_str] = []
            result[date_str].append(assistant)
        
        return result


def set_center_closed_date(closed_date, reason="Holiday / Center Closed"):
    """
    Mark a specific date as center-closed for the owner.
    Returns True if inserted, False if already marked.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        try:
            c.execute(
                """INSERT INTO center_closed_dates (closed_date, reason)
                   VALUES (?, ?, ?)""",
                (closed_date, reason or "Holiday / Center Closed"),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def unset_center_closed_date(closed_date):
    """
    Remove center-closed override for a date.
    Returns number of rows deleted.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """DELETE FROM center_closed_dates
               WHERE closed_date = ?""",
            (closed_date),
        )
        conn.commit()
        return c.rowcount


def is_center_closed_date(closed_date):
    """Return True if a date is explicitly marked center-closed."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        row = c.execute(
            """SELECT 1
               FROM center_closed_dates
               WHERE closed_date = ?
               LIMIT 1""",
            (closed_date),
        ).fetchone()
        return row is not None


def get_center_closed_dates_for_month(year, month):
    """
    Return a set of YYYY-MM-DD strings for center-closed dates in the given month.
    """
    first_day = datetime(year, month, 1).date()
    if month == 12:
        last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)

    start_str = first_day.isoformat()
    end_str = last_day.isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """SELECT closed_date
               FROM center_closed_dates
               WHERE closed_date BETWEEN ? AND ?""",
            (start_str, end_str),
        )
        return {row[0] for row in c.fetchall()}


def unschedule_all_assistants_for_date(scheduled_date):
    """
    Remove all assistant assignments for a specific date.
    Returns number of rows deleted.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """DELETE FROM assistant_schedule
               WHERE scheduled_date = ?""",
            (scheduled_date),
        )
        conn.commit()
        return c.rowcount
