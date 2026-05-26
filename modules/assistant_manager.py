#*****************************
# assistant_manager.py - Assistant management
# Version: 2.2.0
#*****************************
"""
CRUD operations for staff assistants in Stdytime.
"""

import sqlite3
from modules.database import DB_PATH


def _normalize_loading(value, default=1):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        parsed = default
    return max(0, parsed)


def _staff_loading_select(conn: sqlite3.Connection) -> str:
    cols = [row[1] for row in conn.execute("PRAGMA table_info(staff)").fetchall()]
    return "COALESCE(loading, 1) AS loading" if "loading" in cols else "1 AS loading"


def _coerce_blob(raw_value):
    """Convert database BLOB to bytes."""
    if raw_value is None:
        return None
    if isinstance(raw_value, memoryview):
        return raw_value.tobytes()
    if isinstance(raw_value, bytes):
        return raw_value
    return None


def get_all_assistants():
    """Fetch all assistants from database."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        loading_select = _staff_loading_select(conn)
        c.execute(
            f"SELECT id, name, role, email, phone, {loading_select} FROM staff",
            (),
        )
        return c.fetchall()


def get_assistant(assistant_id):
    """Fetch a specific assistant by ID."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        loading_select = _staff_loading_select(conn)
        row = c.execute(
            f"SELECT id, name, role, email, phone, {loading_select} FROM staff WHERE id = ?",
            (assistant_id,),
        ).fetchone()
        return row


def set_assistant_qr_code(assistant_id, qr_blob):
    """Store an assistant's QR code as BLOB in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE staff SET qr_code=? WHERE id=?",
            (sqlite3.Binary(qr_blob) if qr_blob else None, assistant_id),
        )
        conn.commit()


def get_assistant_qr_code(assistant_id):
    """Retrieve an assistant's QR code blob from database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT qr_code FROM staff WHERE id=?",
            (assistant_id,),
        ).fetchone()
        if row and row['qr_code']:
            return _coerce_blob(row['qr_code'])
    return None


def set_assistant_icon(assistant_id, icon_blob=None, icon_mime=''):
    """Store an assistant's icon picture as BLOB in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE staff SET icon_picture=?, icon_picture_mime=? WHERE id=?",
            (sqlite3.Binary(icon_blob) if icon_blob else None, icon_mime or '', assistant_id),
        )
        conn.commit()


def get_assistant_icon(assistant_id):
    """Retrieve an assistant's icon picture blob and mime type from database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT icon_picture, icon_picture_mime FROM staff WHERE id=?",
            (assistant_id,),
        ).fetchone()
        if row and row['icon_picture']:
            return {
                'icon_blob': _coerce_blob(row['icon_picture']),
                'icon_mime': row['icon_picture_mime'] or 'image/png'
            }
    return None


def add_assistant(name, role="", email="", phone="", loading=1):
    """Add a new assistant to the database and automatically generate QR code."""
    loading = _normalize_loading(loading)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO staff (name, role, email, phone, loading) VALUES (?,?,?,?,?)",
            (name, role, email, phone, loading),
        )
        assistant_id = c.lastrowid
        conn.commit()
    
    # Automatically generate QR code for the new assistant and store in DB only if not exists
    try:
        existing_qr = get_assistant_qr_code(assistant_id)
        if not existing_qr:
            from modules import qr_generator
            qr_data = f"ASST:{assistant_id}\nName:{name}"
            qr_blob = qr_generator.generate_qr_bytes(qr_data)
            set_assistant_qr_code(assistant_id, qr_blob)
    except Exception as e:
        print(f"Warning: Failed to generate QR code for assistant {assistant_id}: {e}")
    return assistant_id


def update_assistant(assistant_id, name, role="", email="", phone="", loading=1):
    """Update an existing assistant."""
    loading = _normalize_loading(loading)
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE staff SET name = ?, role = ?, email = ?, phone = ?, loading = ? WHERE id = ?",
            (name, role, email, phone, loading, assistant_id),
        )
        conn.commit()


def delete_assistant(assistant_id):
    """Delete an assistant from the database."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM staff WHERE id = ?", (assistant_id,))
        conn.commit()


def cleanup_old_payroll_data(months=18):
    """
    Delete assistant_sessions (payroll data) older than specified months.
    Default: 18 months data retention policy.
    Returns: Number of records deleted.
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # Count records to be deleted
        c.execute(
                """
                SELECT COUNT(*) FROM assistant_sessions
                WHERE start_time < DATE('now', '-' || ? || ' months')
                """,
                (months,),
            )
        count = c.fetchone()[0]
        
        # Delete old records
        if count > 0:
            c.execute(
                    """
                    DELETE FROM assistant_sessions
                    WHERE start_time < DATE('now', '-' || ? || ' months')
                    """,
                    (months,),
                )
            conn.commit()
            print(f"[Payroll Cleanup] Deleted {count} assistant_sessions records older than {months} months")
        
        return count
