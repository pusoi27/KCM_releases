import os
import sqlite3
import uuid
from datetime import datetime

from modules.database import DB_PATH, issue_unique_qr_token, qr_token_exists, register_qr_token
from modules import qr_generator


def _safe_non_negative_int(value, default=0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 0)


def _has_qr_code(qr_code: str | None) -> bool:
    return bool((qr_code or '').strip())


def _sync_material_availability(cursor, material_id: int):
    row = cursor.execute(
        "SELECT qr_code, copies, borrower_id FROM materials WHERE id = ?",
        (material_id,),
    ).fetchone()
    if not row:
        return
    qr_code, copies, borrower_id = row
    copies = _safe_non_negative_int(copies, default=0)
    available = 1 if (copies > 0 and _has_qr_code(qr_code) and not borrower_id) else 0
    cursor.execute(
        "UPDATE materials SET copies = ?, available = ? WHERE id = ?",
        (copies, available, material_id),
    )


def ensure_material_loans_table():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS material_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                checkout_date TEXT NOT NULL,
                return_date TEXT,
                FOREIGN KEY(material_id) REFERENCES materials(id),
                FOREIGN KEY(student_id) REFERENCES students(id)
            )
            """
        )
        conn.commit()


def _build_material_qr_code(material_id: int) -> str:
    # Global non-reusable token allocation.
    return issue_unique_qr_token("MAT", "material", material_id)


def _coerce_blob(raw_value):
    """Convert database BLOB to bytes."""
    if raw_value is None:
        return None
    if isinstance(raw_value, memoryview):
        return raw_value.tobytes()
    if isinstance(raw_value, bytes):
        return raw_value
    return None


def _ensure_material_qr_image(material_id: int, title: str, qr_code: str, cursor=None):
    """Generate QR code and store in database. If cursor is provided, reuses the existing connection."""
    qr_data = f"MAT:{material_id}\nTitle:{title or ''}\nCode:{qr_code or ''}"
    qr_blob = qr_generator.generate_qr_bytes(qr_data)
    if cursor is not None:
        cursor.execute(
            "UPDATE materials SET qr_code_blob=? WHERE id=?",
            (sqlite3.Binary(qr_blob) if qr_blob else None, material_id),
        )
    else:
        set_material_qr_code_blob(material_id, qr_blob)


def set_material_qr_code_blob(material_id: int, qr_blob):
    """Store a material's QR code as BLOB in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE materials SET qr_code_blob=? WHERE id=?",
            (sqlite3.Binary(qr_blob) if qr_blob else None, material_id),
        )
        conn.commit()


def get_material_qr_code_blob(material_id: int):
    """Retrieve a material's QR code blob from database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT qr_code_blob FROM materials WHERE id=?",
            (material_id,),
        ).fetchone()
        if row and row['qr_code_blob']:
            return _coerce_blob(row['qr_code_blob'])
    return None


def get_materials():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, author, available, reading_level, qr_code, publisher, copies, borrower_id
            FROM materials
            ORDER BY title
            """
        )
        return c.fetchall()


def get_material(material_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, author, available, reading_level, qr_code, publisher, copies, borrower_id
            FROM materials
            WHERE id = ?
            """,
            (material_id,),
        )
        return c.fetchone()


def add_material(title, author, publisher, qr_code=None, available=1, reading_level=None, copies=1):
    copies = _safe_non_negative_int(copies, default=1)
    qr_code = (qr_code or '').strip() or None
    if qr_code and qr_token_exists(qr_code):
        raise ValueError("QR code already issued and cannot be reused.")
    if copies == 0:
        qr_code = None
    available = 1 if (copies > 0 and _has_qr_code(qr_code)) else 0

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO materials (title, author, publisher, qr_code, available, reading_level, copies)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, author, publisher, qr_code, available, reading_level, copies),
        )
        material_id = c.lastrowid

        # Only generate QR code if not already present
        existing_qr = get_material_qr_code_blob(material_id)
        if not existing_qr:
            if not qr_code:
                qr_code = _build_material_qr_code(material_id)
                c.execute("UPDATE materials SET qr_code = ? WHERE id = ?", (qr_code, material_id))
            _ensure_material_qr_image(material_id, title, qr_code, cursor=c)
            if qr_code:
                register_qr_token(qr_code, "material", material_id, retired=0)
        _sync_material_availability(c, material_id)
        conn.commit()
        return material_id


def update_material(material_id, title=None, author=None, publisher=None, qr_code=None, available=None, reading_level=None, copies=None, borrower_id=None):
    updates = []
    params = []

    if title is not None:
        updates.append("title = ?")
        params.append(title)
    if author is not None:
        updates.append("author = ?")
        params.append(author)
    if publisher is not None:
        updates.append("publisher = ?")
        params.append(publisher)
    if qr_code is not None:
        qr_candidate = str(qr_code or '').strip()
        if qr_candidate:
            with sqlite3.connect(DB_PATH) as _conn:
                _cur = _conn.cursor()
                existing_row = _cur.execute("SELECT qr_code FROM materials WHERE id = ?", (material_id,)).fetchone()
                existing_qr_value = str((existing_row[0] if existing_row else '') or '').strip()
            if qr_candidate != existing_qr_value and qr_token_exists(qr_candidate):
                raise ValueError("QR code already issued and cannot be reused.")
        updates.append("qr_code = ?")
        params.append(qr_candidate)
    if available is not None:
        updates.append("available = ?")
        params.append(available)
    if reading_level is not None:
        updates.append("reading_level = ?")
        params.append(reading_level)
    if copies is not None:
        updates.append("copies = ?")
        params.append(_safe_non_negative_int(copies, default=0))
    if borrower_id is not None:
        updates.append("borrower_id = ?")
        params.append(borrower_id)

    if not updates:
        return False

    params.append(material_id)
    query = f"UPDATE materials SET {', '.join(updates)} WHERE id = ?"

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(query, params)

        row = c.execute("SELECT title, qr_code, copies FROM materials WHERE id = ?", (material_id,)).fetchone()
        if row:
            title_value, qr_code_value, _ = row
            if not _has_qr_code(qr_code_value):
                qr_code_value = _build_material_qr_code(material_id)
                c.execute("UPDATE materials SET qr_code = ? WHERE id = ?", (qr_code_value, material_id))
            _ensure_material_qr_image(material_id, title_value or '', qr_code_value, cursor=c)
            if qr_code_value:
                register_qr_token(str(qr_code_value).strip(), "material", material_id, retired=0)
            _sync_material_availability(c, material_id)

        conn.commit()
        return c.rowcount > 0


def delete_material(material_id):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM materials WHERE id = ?", (material_id,))
        conn.commit()
        return c.rowcount > 0


def enforce_qr_availability_rule():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            UPDATE materials
               SET available = 0
             WHERE COALESCE(copies, 0) <= 0
            """
        )
        c.execute(
            """
            UPDATE materials
               SET available = 0
             WHERE (qr_code IS NULL OR TRIM(qr_code) = '')
               AND COALESCE(copies, 0) > 0
            """
        )
        c.execute(
            """
            UPDATE materials
               SET available = 1
             WHERE (available IS NULL OR available = 0)
               AND (qr_code IS NOT NULL AND TRIM(qr_code) != '')
               AND COALESCE(copies, 0) > 0
               AND borrower_id IS NULL
            """
        )
        conn.commit()


def find_material_by_title(title: str):
    if not title:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, author, available, reading_level, qr_code, publisher, copies, borrower_id
            FROM materials
            WHERE lower(title) = lower(?)
            LIMIT 1
            """,
            (title.strip(),),
        )
        return c.fetchone()


def find_material_by_qr_code(qr_code: str):
    if not qr_code:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, author, available, reading_level, qr_code, publisher, copies, borrower_id
            FROM materials
            WHERE qr_code = ?
            LIMIT 1
            """,
            (qr_code.strip(),),
        )
        return c.fetchone()


def loan_material(material_id: int, student_id: int):
    ensure_material_loans_table()
    checkout_date = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        student_exists = c.execute("SELECT id FROM students WHERE id = ?", (student_id,)).fetchone()
        if not student_exists:
            return None

        c.execute("UPDATE materials SET available = 0, borrower_id = ? WHERE id = ?", (student_id, material_id))
        if c.rowcount == 0:
            return None

        c.execute("UPDATE students SET device_loaned = 1 WHERE id = ?", (student_id,))
        c.execute(
            """
            INSERT INTO material_loans (material_id, student_id, checkout_date)
            VALUES (?, ?, ?)
            """,
            (material_id, student_id, checkout_date),
        )
        conn.commit()
        return checkout_date


def return_material(material_id: int):
    ensure_material_loans_table()
    return_date = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        borrower_row = c.execute("SELECT borrower_id FROM materials WHERE id = ?", (material_id,)).fetchone()
        borrower_id = borrower_row[0] if borrower_row else None
        c.execute("UPDATE materials SET borrower_id = NULL WHERE id = ?", (material_id,))
        _sync_material_availability(c, material_id)
        if borrower_id:
            remaining = c.execute(
                "SELECT COUNT(*) FROM materials WHERE borrower_id = ?", (borrower_id,)
            ).fetchone()[0]
            if remaining == 0:
                c.execute("UPDATE students SET device_loaned = 0 WHERE id = ?", (borrower_id,))
        c.execute(
            """
            UPDATE material_loans
            SET return_date = ?
            WHERE id = (
                SELECT id
                FROM material_loans
                WHERE material_id = ?
                  AND return_date IS NULL
                ORDER BY checkout_date DESC
                LIMIT 1
            )
            """,
            (return_date, material_id),
        )

        conn.commit()
        return return_date


def clear_active_material_loan(material_id: int, student_id: int):
    ensure_material_loans_table()
    return_date = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        open_loan = c.execute(
            """
            SELECT id
            FROM material_loans
            WHERE material_id = ?
              AND student_id = ?
              AND return_date IS NULL
            ORDER BY checkout_date DESC
            LIMIT 1
            """,
            (material_id, student_id),
        ).fetchone()
        if not open_loan:
            return None

        c.execute("UPDATE material_loans SET return_date = ? WHERE id = ?", (return_date, open_loan[0]))
        c.execute("UPDATE materials SET borrower_id = NULL WHERE id = ?", (material_id,))
        _sync_material_availability(c, material_id)
        remaining = c.execute(
            "SELECT COUNT(*) FROM materials WHERE borrower_id = ?", (student_id,)
        ).fetchone()[0]
        if remaining == 0:
            c.execute("UPDATE students SET device_loaned = 0 WHERE id = ?", (student_id,))
        conn.commit()
        return return_date


def get_loaned_materials_detailed():
    ensure_material_loans_table()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        rows = c.execute(
            """
            SELECT s.name, m.title, ml.checkout_date, s.id, m.id, ml.id
            FROM material_loans ml
            JOIN materials m ON ml.material_id = m.id
            JOIN students s ON ml.student_id = s.id
            WHERE ml.return_date IS NULL
            ORDER BY s.name, ml.checkout_date
            """
        ).fetchall()

        copies_rows = c.execute(
            """
            SELECT lower(trim(COALESCE(title, ''))) AS title_key,
                   COALESCE(SUM(COALESCE(copies, 0)), 0) AS total_copies
            FROM materials
            GROUP BY lower(trim(COALESCE(title, '')))
            """
        ).fetchall()
        copies_by_title = {row[0]: row[1] for row in copies_rows}

        active_rows = c.execute(
            """
            SELECT lower(trim(COALESCE(m.title, ''))) AS title_key,
                   COUNT(*) AS active_loans
            FROM material_loans ml
            JOIN materials m ON ml.material_id = m.id
            WHERE ml.return_date IS NULL
            GROUP BY lower(trim(COALESCE(m.title, '')))
            """
        ).fetchall()
        active_by_title = {row[0]: row[1] for row in active_rows}

        detailed = []
        for student_name, material_title, checkout_date, student_id, material_id, loan_id in rows:
            title_key = (material_title or '').strip().lower()
            total_copies = int(copies_by_title.get(title_key, 0) or 0)
            active_loans = int(active_by_title.get(title_key, 0) or 0)
            detailed.append(
                {
                    'student_name': student_name,
                    'material_title': material_title,
                    'checkout_date': checkout_date,
                    'student_id': student_id,
                    'material_id': material_id,
                    'loan_id': loan_id,
                    'show_clear': total_copies < active_loans,
                }
            )
        return detailed


def sync_all_students_material_status():
    """Parity endpoint for books; devices currently don't mutate a student status flag."""
    ensure_material_loans_table()
    return 0
