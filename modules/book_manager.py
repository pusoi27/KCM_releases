#*****************************
#book_manager.py   ver 04--
#*****************************
import sqlite3
from datetime import datetime
import requests

from modules.database import DB_PATH


BOOK_COVER_FETCH_TIMEOUT_SECONDS = 8
BOOK_COVER_MAX_BYTES = 128 * 1024


def _has_isbn(isbn, isbn13) -> bool:
    """Return True when either ISBN-10 or ISBN-13 is present."""
    return bool((isbn and str(isbn).strip()) or (isbn13 and str(isbn13).strip()))


def _safe_non_negative_int(value, default=0) -> int:
    """Parse value as a non-negative integer with fallback."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 0)


def _normalize_isbn_token(value: str | None) -> str | None:
    token = ''.join(ch for ch in str(value or '').upper() if ch.isdigit() or ch == 'X')
    return token or None


def _is_valid_isbn10(token: str | None) -> bool:
    code = _normalize_isbn_token(token)
    if not code or len(code) != 10:
        return False
    if not code[:9].isdigit():
        return False
    if not (code[9].isdigit() or code[9] == 'X'):
        return False

    total = 0
    for idx, ch in enumerate(code[:9]):
        total += (10 - idx) * int(ch)
    total += 10 if code[9] == 'X' else int(code[9])
    return total % 11 == 0


def _is_valid_isbn13(token: str | None) -> bool:
    code = _normalize_isbn_token(token)
    if not code or len(code) != 13 or not code.isdigit():
        return False

    total = 0
    for idx, ch in enumerate(code[:12]):
        n = int(ch)
        total += n if idx % 2 == 0 else n * 3
    check = (10 - (total % 10)) % 10
    return check == int(code[12])


def _preferred_valid_isbn(isbn: str | None, isbn13: str | None) -> str | None:
    code13 = _normalize_isbn_token(isbn13)
    if _is_valid_isbn13(code13):
        return code13

    code10 = _normalize_isbn_token(isbn)
    if _is_valid_isbn10(code10):
        return code10

    return None


def _fetch_small_cover_blob_from_openlibrary(isbn_token: str) -> tuple[bytes | None, str]:
    """Return (blob, mime) for a small cover image, or (None, '')."""
    if not isbn_token:
        return None, ''

    url = f"https://covers.openlibrary.org/b/isbn/{isbn_token}-S.jpg"
    params = {"default": "false"}
    headers = {"User-Agent": "Stdytime/1.0"}

    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=(3, BOOK_COVER_FETCH_TIMEOUT_SECONDS),
        )
    except requests.RequestException:
        return None, ''

    if response.status_code != 200:
        return None, ''

    mime = str(response.headers.get('Content-Type') or '').split(';', 1)[0].strip().lower()
    if not mime.startswith('image/'):
        return None, ''

    payload = response.content or b''
    if not payload or len(payload) > BOOK_COVER_MAX_BYTES:
        return None, ''

    return payload, mime or 'image/jpeg'


def _sync_student_book_loaned(cursor, student_id: int):
    """Sync students.book_loaned based on current open book loans."""
    if not student_id:
        return
    cursor.execute(
        """
        UPDATE students
           SET book_loaned = CASE WHEN EXISTS (
               SELECT 1
                 FROM books
                WHERE borrower_id = ?
                  AND available = 0
           ) THEN 1 ELSE 0 END
         WHERE id = ?
        """,
        (student_id, student_id),
    )


def sync_all_students_book_status():
    """Sync book_loaned status for all students based on current book loans."""
    ensure_book_loans_table()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        c.execute("UPDATE students SET book_loaned = 0")
        c.execute(
            """
            UPDATE students
            SET book_loaned = 1
            WHERE id IN (
                SELECT DISTINCT bl.student_id
                FROM book_loans bl
                JOIN students s ON s.id = bl.student_id
                WHERE bl.return_date IS NULL
            )
            """,
            (),
        )

        c.execute(
            """
            UPDATE books
            SET borrower_id = NULL,
                available = CASE
                    WHEN COALESCE(copies, 0) > 0
                     AND ((isbn IS NOT NULL AND TRIM(isbn) != '') OR (isbn13 IS NOT NULL AND TRIM(isbn13) != ''))
                    THEN 1 ELSE 0 END
            WHERE id NOT IN (
                SELECT bl.book_id
                FROM book_loans bl
                JOIN books b ON b.id = bl.book_id
                WHERE bl.return_date IS NULL
            )
            """,
            (),
        )

        c.execute(
            """
            UPDATE books
            SET borrower_id = (
                SELECT bl.student_id
                FROM book_loans bl
                JOIN students s ON s.id = bl.student_id
                WHERE bl.book_id = books.id
                  AND bl.return_date IS NULL
                ORDER BY bl.checkout_date DESC
                LIMIT 1
            ),
            available = 0
            WHERE id IN (
                SELECT bl.book_id
                FROM book_loans bl
                JOIN books b ON b.id = bl.book_id
                JOIN students s ON s.id = bl.student_id
                WHERE bl.return_date IS NULL
            )
            """,
            (),
        )

        conn.commit()
        return c.rowcount


def get_books():
    """Get all books with all columns."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id
            FROM books
            ORDER BY title
            """,
            (),
        )
        return c.fetchall()


def get_book(book_id):
    """Get a single book by ID."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id
            FROM books
            WHERE id = ?
            """,
            (book_id,),
        )
        return c.fetchone()


def _coerce_blob(raw_value):
    """Convert database BLOB to bytes."""
    if raw_value is None:
        return None
    if isinstance(raw_value, memoryview):
        return raw_value.tobytes()
    if isinstance(raw_value, bytes):
        return raw_value
    return None


def set_book_qr_code(book_id, qr_blob):
    """Store a book's QR code as BLOB in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE books SET qr_code_blob=? WHERE id=?",
            (sqlite3.Binary(qr_blob) if qr_blob else None, book_id),
        )
        conn.commit()


def get_book_qr_code(book_id):
    """Retrieve a book's QR code blob from database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT qr_code_blob FROM books WHERE id=?",
            (book_id,),
        ).fetchone()
        if row and row['qr_code_blob']:
            return _coerce_blob(row['qr_code_blob'])
    return None


def set_book_cover(book_id, cover_blob, cover_mime=''):
    """Store a book cover as BLOB in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE books SET cover_blob=?, cover_mime=? WHERE id=?",
            (sqlite3.Binary(cover_blob) if cover_blob else None, (cover_mime or ''), book_id),
        )
        conn.commit()


def get_book_cover(book_id):
    """Retrieve a book cover blob from database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT cover_blob, COALESCE(cover_mime, '') AS cover_mime FROM books WHERE id=?",
            (book_id,),
        ).fetchone()
        if row and row['cover_blob']:
            return {
                'cover_blob': _coerce_blob(row['cover_blob']),
                'cover_mime': str(row['cover_mime'] or '') or 'image/jpeg',
            }
    return None


def add_book(title, author, publisher, isbn=None, isbn13=None, available=1, reading_level=None, copies=1):
    """Add a new book to the database."""
    copies = _safe_non_negative_int(copies, default=1)

    if copies == 0:
        isbn = None
        isbn13 = None

    available = 1 if (copies > 0 and _has_isbn(isbn, isbn13)) else 0

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO books (title, author, publisher, isbn, isbn13, available, reading_level, copies)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, author, publisher, isbn, isbn13, available, reading_level, copies),
        )
        book_id = c.lastrowid
        # Only generate QR code if not already present
        existing_qr = get_book_qr_code(book_id)
        if not existing_qr:
            # If you have a QR code generation logic, add it here
            pass  # No QR code generation if one exists

        valid_isbn = _preferred_valid_isbn(isbn, isbn13)
        if valid_isbn:
            cover_blob, cover_mime = _fetch_small_cover_blob_from_openlibrary(valid_isbn)
            if cover_blob:
                c.execute(
                    "UPDATE books SET cover_blob = ?, cover_mime = ? WHERE id = ?",
                    (sqlite3.Binary(cover_blob), cover_mime or 'image/jpeg', book_id),
                )

        conn.commit()
        return book_id


def update_book(book_id, title=None, author=None, publisher=None, isbn=None, isbn13=None, available=None, reading_level=None, copies=None, borrower_id=None):
    """Update a book."""
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
    if isbn is not None:
        updates.append("isbn = ?")
        params.append(isbn)
    if isbn13 is not None:
        updates.append("isbn13 = ?")
        params.append(isbn13)
    if available is not None:
        updates.append("available = ?")
        params.append(available)
    if reading_level is not None:
        updates.append("reading_level = ?")
        params.append(reading_level)
    if copies is not None:
        updates.append("copies = ?")
        params.append(copies)
    if borrower_id is not None:
        updates.append("borrower_id = ?")
        params.append(borrower_id)

    if not updates:
        return False

    params.extend([book_id])
    query = f"UPDATE books SET {', '.join(updates)} WHERE id = ?"

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(query, params)

        row = c.execute(
            "SELECT isbn, isbn13, copies, borrower_id FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()

        if row:
            current_isbn, current_isbn13, current_copies, current_borrower_id = row
            current_copies = _safe_non_negative_int(current_copies, default=0)

            if current_copies == 0:
                c.execute(
                    """
                    UPDATE books
                       SET copies = 0,
                           isbn = NULL,
                           isbn13 = NULL,
                           available = 0
                     WHERE id = ?
                    """,
                    (book_id,),
                )
            else:
                desired_available = 1 if (_has_isbn(current_isbn, current_isbn13) and not current_borrower_id) else 0
                c.execute(
                    "UPDATE books SET copies = ?, available = ? WHERE id = ?",
                    (current_copies, desired_available, book_id),
                )

        conn.commit()
        return c.rowcount > 0


def delete_book(book_id):
    """Delete a book."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM books WHERE id = ?", (book_id,))
        conn.commit()
        return c.rowcount > 0


def enforce_isbn_availability_rule():
    """Normalize availability state according to ISBN/copies/borrower rules."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            UPDATE books
               SET copies = 0,
                   isbn = NULL,
                   isbn13 = NULL,
                   available = 0
             WHERE COALESCE(copies, 0) <= 0
            """,
            (),
        )
        c.execute(
            """
            UPDATE books
               SET available = 0
             WHERE (isbn IS NULL OR TRIM(isbn) = '')
               AND (isbn13 IS NULL OR TRIM(isbn13) = '')
               AND COALESCE(copies, 0) > 0
            """,
            (),
        )
        c.execute(
            """
            UPDATE books
               SET available = 1
             WHERE (available IS NULL OR available = 0)
               AND ((isbn IS NOT NULL AND TRIM(isbn) != '') OR (isbn13 IS NOT NULL AND TRIM(isbn13) != ''))
               AND COALESCE(copies, 0) > 0
               AND borrower_id IS NULL
            """,
            (),
        )
        conn.commit()


def ensure_book_loans_table():
    """Create book_loans table if it does not exist."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS book_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                checkout_date TEXT NOT NULL,
                return_date TEXT,
                FOREIGN KEY(book_id) REFERENCES books(id),
                FOREIGN KEY(student_id) REFERENCES students(id)
            )
            """
        )
        conn.commit()


def loan_book(book_id: int, student_id: int):
    """Mark a book as loaned to a student and record the checkout date."""
    ensure_book_loans_table()
    checkout_date = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        student_exists = c.execute(
            "SELECT id FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        if not student_exists:
            return None

        c.execute(
            "UPDATE books SET available = 0, borrower_id = ? WHERE id = ?",
            (student_id, book_id),
        )
        if c.rowcount == 0:
            return None

        c.execute(
            """
            INSERT INTO book_loans (book_id, student_id, checkout_date)
            VALUES (?, ?, ?)
            """,
            (book_id, student_id, checkout_date),
        )
        _sync_student_book_loaned(c, student_id)
        conn.commit()
        return checkout_date


def return_book(book_id: int):
    """Mark a book as returned and set return_date for the open loan."""
    ensure_book_loans_table()
    return_date = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        borrower_row = c.execute(
            "SELECT borrower_id FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        borrower_id = borrower_row[0] if borrower_row else None

        c.execute(
            "UPDATE books SET available = 1, borrower_id = NULL WHERE id = ?",
            (book_id,),
        )

        c.execute(
            """
            UPDATE book_loans
            SET return_date = ?
            WHERE id = (
                SELECT id FROM book_loans
                WHERE book_id = ?
                  AND return_date IS NULL
                                ORDER BY checkout_date DESC
                LIMIT 1
            )
            """,
            (return_date, book_id),
        )
        _sync_student_book_loaned(c, borrower_id)
        conn.commit()
        return return_date


def search_books(query=None, level=None, available_only=False):
    """Search books by various criteria."""
    sql = "SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id FROM books WHERE 1=1"
    params = []
    if query:
        sql += " AND (title LIKE ? OR author LIKE ? OR publisher LIKE ?)"
        search_term = f"%{query}%"
        params.extend([search_term, search_term, search_term])

    if level:
        sql += " AND reading_level = ?"
        params.append(level)

    if available_only:
        sql += " AND available = 1"

    sql += " ORDER BY title"

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(sql, params)
        return c.fetchall()


def get_loaned_books():
    """Get all currently loaned books with student information."""
    ensure_book_loans_table()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT s.name, b.title, bl.checkout_date, s.id
            FROM book_loans bl
            JOIN books b ON bl.book_id = b.id
            JOIN students s ON bl.student_id = s.id
            WHERE bl.return_date IS NULL
            ORDER BY s.name, bl.checkout_date
            """,
            (),
        )
        return c.fetchall()


def get_loaned_books_detailed():
    """Get active loans with per-row clear-loan eligibility."""
    ensure_book_loans_table()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        loan_rows = c.execute(
            """
            SELECT s.name, b.title, bl.checkout_date, s.id, b.id, bl.id
            FROM book_loans bl
            JOIN books b ON bl.book_id = b.id
            JOIN students s ON bl.student_id = s.id
            WHERE bl.return_date IS NULL
            ORDER BY s.name, bl.checkout_date
            """,
            (),
        ).fetchall()

        copies_rows = c.execute(
            """
            SELECT lower(trim(COALESCE(title, ''))) AS title_key,
                   COALESCE(SUM(COALESCE(copies, 0)), 0) AS total_copies
              FROM books
             GROUP BY lower(trim(COALESCE(title, '')))
            """,
            (),
        ).fetchall()
        copies_by_title = {row[0]: row[1] for row in copies_rows}

        active_loan_rows = c.execute(
            """
            SELECT lower(trim(COALESCE(b.title, ''))) AS title_key,
                   COUNT(*) AS active_loans
              FROM book_loans bl
              JOIN books b ON bl.book_id = b.id
             WHERE bl.return_date IS NULL
             GROUP BY lower(trim(COALESCE(b.title, '')))
            """,
            (),
        ).fetchall()
        active_loans_by_title = {row[0]: row[1] for row in active_loan_rows}

        detailed = []
        for student_name, book_title, checkout_date, student_id, book_id, loan_id in loan_rows:
            title_key = (book_title or "").strip().lower()
            total_copies = int(copies_by_title.get(title_key, 0) or 0)
            active_loans = int(active_loans_by_title.get(title_key, 0) or 0)
            detailed.append(
                {
                    "student_name": student_name,
                    "book_title": book_title,
                    "checkout_date": checkout_date,
                    "student_id": student_id,
                    "book_id": book_id,
                    "loan_id": loan_id,
                    "show_clear": total_copies < active_loans,
                }
            )

        return detailed


def clear_active_loan(book_id: int, student_id: int):
    """Clear an active loan for a specific student/book pair."""
    ensure_book_loans_table()
    return_date = datetime.utcnow().isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        open_loan = c.execute(
            """
            SELECT id
            FROM book_loans
            WHERE book_id = ?
              AND student_id = ?
              AND return_date IS NULL
                        ORDER BY checkout_date DESC
            LIMIT 1
            """,
            (book_id, student_id),
        ).fetchone()

        if not open_loan:
            return None

        loan_id = open_loan[0]
        c.execute("UPDATE book_loans SET return_date = ? WHERE id = ?", (return_date, loan_id))

        book_row = c.execute(
            "SELECT isbn, isbn13, copies FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()

        if book_row:
            isbn, isbn13, copies = book_row
            copies = _safe_non_negative_int(copies, default=0)
            available = 1 if (copies > 0 and _has_isbn(isbn, isbn13)) else 0
            c.execute(
                "UPDATE books SET borrower_id = NULL, available = ? WHERE id = ?",
                (available, book_id),
            )

        _sync_student_book_loaned(c, student_id)
        conn.commit()
        return return_date


def find_book_by_title(title: str):
    """Find a single book by exact title (case-insensitive)."""
    if not title:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id "
            "FROM books WHERE lower(title) = lower(?) LIMIT 1",
            (title.strip(),),
        )
        return c.fetchone()


def find_book_by_isbn(isbn: str):
    """Find a single book by ISBN (checks both ISBN-10 and ISBN-13)."""
    if not isbn:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id "
            "FROM books WHERE (isbn = ? OR isbn13 = ?) LIMIT 1",
            (isbn.strip(), isbn.strip()),
        )
        return c.fetchone()