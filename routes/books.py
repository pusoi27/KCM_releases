# routes/books.py
"""Books management routes."""

from flask import render_template, request, jsonify, redirect, url_for, flash, g
from modules.book_manager import (
    get_books,
    find_book_by_title,
    find_book_by_isbn,
    add_book,
    update_book,
    get_book,
    delete_book,
    loan_book,
    return_book,
    clear_active_loan,
    get_loaned_books_detailed,
    enforce_isbn_availability_rule,
    sync_all_students_book_status,
)
from modules.student_manager import get_student
from modules import server_cache, db_backup_recovery, auth_manager
from modules import scanner_sync
from routes.auth import require_login, require_admin, require_feature
from routes.operation_utils import invalidate_scoped_cache, json_scoped_failure
import sqlite3
from modules.database import DB_PATH, sync_to_gdrive_now, get_station_runtime_config
import requests
import re
import time
from threading import Lock


BOOK_LEVEL_ORDER = ["1", "2", "3", "4", "5", "6", "7", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"]
ISBN_LOOKUP_CACHE_TTL_SECONDS = 12 * 60 * 60
ISBN_LOOKUP_RATE_LIMIT_COOLDOWN_KEY = "books:isbn_lookup:rate_limited:v1"
BRIDGE_GET_TIMEOUT_SECONDS = 4
BRIDGE_POST_TIMEOUT_SECONDS = 5
_isbn_lookup_locks = {}
_isbn_lookup_locks_guard = Lock()


def _runtime_station_mode() -> str:
    return str(get_station_runtime_config().get("station_mode") or "").strip().lower() or "instructor_server"


def _runtime_instructor_api_base() -> str:
    return str(get_station_runtime_config().get("instructor_api_base_url") or "").strip().rstrip("/")


def _runtime_pairing_token() -> str:
    return str(get_station_runtime_config().get("station_pairing_token") or "").strip()


def _scanner_api_client_enabled() -> bool:
    return _runtime_station_mode() == "scanner_api_client"


def _bridge_headers() -> dict:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = _runtime_pairing_token()
    if token:
        headers["X-Stdytime-Pairing-Token"] = token
    return headers


def _bridge_url(path: str) -> str:
    base = _runtime_instructor_api_base()
    route = str(path or "").strip()
    if not route.startswith("/"):
        route = "/" + route
    return f"{base}{route}"


def _bridge_forward_get(path: str, params: dict | None = None):
    if not _scanner_api_client_enabled():
        return None

    url = _bridge_url(path)
    if not url.startswith("http"):
        g.scanner_bridge_fallback = True
        g.scanner_bridge_fallback_reason = "missing_instructor_api_url"
        return None
    try:
        response = requests.get(url, headers=_bridge_headers(), params=(params or {}), timeout=(2, BRIDGE_GET_TIMEOUT_SECONDS))
        payload = response.json()
    except requests.RequestException as exc:
        g.scanner_bridge_fallback = True
        g.scanner_bridge_fallback_reason = f"request_error:{exc}"
        return None
    except Exception:
        payload = {"error": f"Bridge returned non-JSON response (HTTP {response.status_code})."}
    return jsonify(payload), int(response.status_code)


def _bridge_forward_post(path: str, payload: dict | None = None):
    if not _scanner_api_client_enabled():
        return None

    url = _bridge_url(path)
    if not url.startswith("http"):
        g.scanner_bridge_fallback = True
        g.scanner_bridge_fallback_reason = "missing_instructor_api_url"
        return None
    try:
        response = requests.post(url, headers=_bridge_headers(), json=(payload or {}), timeout=(2, BRIDGE_POST_TIMEOUT_SECONDS))
        body = response.json()
    except requests.RequestException as exc:
        g.scanner_bridge_fallback = True
        g.scanner_bridge_fallback_reason = f"request_error:{exc}"
        return None
    except Exception:
        body = {"error": f"Bridge returned non-JSON response (HTTP {response.status_code})."}
    return jsonify(body), int(response.status_code)


def _bridge_request_is_pairing_authorized() -> bool:
    expected = _runtime_pairing_token()
    if not expected:
        return False
    received = str(request.headers.get("X-Stdytime-Pairing-Token") or "").strip()
    return bool(received and received == expected)


def _bridge_pairing_auth_error(message: str = "Invalid or missing pairing token."):
    return jsonify({"error": message}), 401


class IsbnLookupError(RuntimeError):
    """Structured lookup error with HTTP status metadata."""

    def __init__(self, message: str, status_code: int = 502, retry_after_seconds: int = 0):
        super().__init__(message)
        self.status_code = int(status_code)
        self.retry_after_seconds = max(0, int(retry_after_seconds or 0))


def _parse_non_negative_int(value, default=0):
    """Parse numeric payload values as non-negative integers."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 0)


def _books_catalog_cache_key() -> str:
    return server_cache.BOOKS_CATALOG_CACHE_KEY

def _book_detail_cache_key(book_id) -> str:
    return f"books:detail:v2:{book_id}"

def _book_detail_prefix() -> str:
    return "books:detail:v2:"

def _students_list_cache_key() -> str:
    return server_cache.STUDENTS_LIST_CACHE_KEY


def _isbn_lookup_cache_key(isbn: str) -> str:
    return f"books:isbn_lookup:v1:{isbn}"


def _get_isbn_lookup_lock(isbn: str) -> Lock:
    with _isbn_lookup_locks_guard:
        lock = _isbn_lookup_locks.get(isbn)
        if lock is None:
            lock = Lock()
            _isbn_lookup_locks[isbn] = lock
        return lock


def _cache_isbn_lookup_result(isbn: str, data: dict) -> None:
    """Cache lookup result for requested ISBN and discovered ISBN aliases."""
    aliases = {str(isbn or '').strip()}
    if isinstance(data, dict):
        aliases.add(str(data.get('isbn') or '').strip())
        aliases.add(str(data.get('isbn13') or '').strip())

    for alias in aliases:
        alias = _sanitize_isbn(alias)
        if not alias:
            continue
        server_cache.set_cache(
            _isbn_lookup_cache_key(alias),
            data,
            policy="book_catalog",
            ttl_seconds=ISBN_LOOKUP_CACHE_TTL_SECONDS,
        )


def _lookup_isbn_online_cached(isbn: str) -> dict:
    """Single-flight cached lookup to ensure one Google call per ISBN key."""
    cached = server_cache.get_cache(_isbn_lookup_cache_key(isbn))
    if cached is not None:
        return cached

    lock = _get_isbn_lookup_lock(isbn)
    with lock:
        cached = server_cache.get_cache(_isbn_lookup_cache_key(isbn))
        if cached is not None:
            return cached

        data = _lookup_isbn_online(isbn)
        _cache_isbn_lookup_result(isbn, data)
        return data

def _invalidate_books_cache(book_id=None):
    """Invalidate books catalog lane and optionally one book detail lane."""
    server_cache.invalidate(_books_catalog_cache_key())
    if book_id is not None:
        try:
            server_cache.invalidate(_book_detail_cache_key(book_id))
        except (TypeError, ValueError):
            pass


def _invalidate_book_sync_caches():
    """Invalidate all caches touched by book/student sync."""
    server_cache.invalidate(_students_list_cache_key())
    server_cache.invalidate(_books_catalog_cache_key())
    server_cache.invalidate_prefix(_book_detail_prefix())


def register_book_routes(app):
    """Register book management routes."""

    @app.route('/api/bridge/books/catalog')
    def api_bridge_books_catalog():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            enforce_isbn_availability_rule()
            rows = get_books()
            books_payload = [_book_row_to_dict(row) for row in rows]
            return jsonify({'books': books_payload, 'count': len(books_payload)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/levels')
    def api_bridge_books_levels():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT DISTINCT reading_level FROM books WHERE reading_level IS NOT NULL")
            raw_levels = [str(row[0]).strip() for row in c.fetchall() if str(row[0] or '').strip()]
            conn.close()

            level_rank = {level: idx for idx, level in enumerate(BOOK_LEVEL_ORDER)}
            normalized = []
            seen = set()
            for level in raw_levels:
                if level in seen:
                    continue
                seen.add(level)
                normalized.append(level)

            levels = sorted(
                normalized,
                key=lambda lv: (level_rank.get(lv, len(BOOK_LEVEL_ORDER)), lv)
            )
            return jsonify({'levels': levels})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/isbn_lookup')
    def api_bridge_books_isbn_lookup():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        isbn_raw = (request.args.get('isbn') or '').strip()
        isbn = _sanitize_isbn(isbn_raw)

        if not isbn or len(isbn) not in (10, 13):
            return jsonify({'error': 'Please scan or enter a valid ISBN-10 or ISBN-13.'}), 400

        cooldown = server_cache.get_cache(ISBN_LOOKUP_RATE_LIMIT_COOLDOWN_KEY)
        if cooldown:
            retry_after = int(cooldown.get('retry_after_seconds', 5)) if isinstance(cooldown, dict) else 5
            return jsonify({
                'error': 'Google Books is rate-limiting requests. Please retry shortly.',
                'retry_after_seconds': retry_after,
            }), 429

        try:
            data = _lookup_isbn_online_cached(isbn)
        except IsbnLookupError as e:
            if e.status_code == 429:
                retry_after = max(1, e.retry_after_seconds or 5)
                server_cache.set_cache(
                    ISBN_LOOKUP_RATE_LIMIT_COOLDOWN_KEY,
                    {'retry_after_seconds': retry_after},
                    ttl_seconds=retry_after,
                )
                return jsonify({
                    'error': 'Google Books is rate-limiting requests. Please retry shortly.',
                    'retry_after_seconds': retry_after,
                }), 429
            return jsonify({'error': f"Lookup failed: {e}"}), 502
        except Exception as e:
            return jsonify({'error': f"Lookup failed: {e}"}), 502

        existing_by_isbn = find_book_by_isbn(isbn)
        isbn_existing_id = existing_by_isbn[0] if existing_by_isbn else None
        isbn_existing_book = None
        if isbn_existing_id:
            isbn_existing_book = get_book(isbn_existing_id)

        existing = find_book_by_title(data.get('title')) if data.get('title') else None
        existing_id = existing[0] if existing else None
        existing_book = None
        if existing_id:
            existing_book = get_book(existing_id)

        return jsonify({
            'book': data,
            'existing_id': existing_id,
            'existing_book': _book_row_to_dict(existing_book) if existing_book else None,
            'isbn_existing_id': isbn_existing_id,
            'isbn_existing_book': _book_row_to_dict(isbn_existing_book) if isbn_existing_book else None,
            'message': "Book found" if data else "No data found",
        })

    @app.route('/api/bridge/books/search')
    def api_bridge_books_search():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        query_raw = request.args.get('q', '').strip()
        query = query_raw.lower()
        level = request.args.get('level', '').strip()
        isbn_candidates = _expand_isbn_candidates(query_raw)

        try:
            enforce_isbn_availability_rule()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()

            sql = "SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id FROM books WHERE 1=1"
            params = []

            if isbn_candidates:
                placeholders = ",".join(["?" for _ in isbn_candidates])
                sql += (
                    f" AND (UPPER(TRIM(COALESCE(isbn, ''))) IN ({placeholders}) "
                    f"OR UPPER(TRIM(COALESCE(isbn13, ''))) IN ({placeholders}))"
                )
                params.extend(isbn_candidates)
                params.extend(isbn_candidates)
            elif query:
                sql += " AND (title LIKE ? OR author LIKE ? OR publisher LIKE ? OR isbn LIKE ? OR isbn13 LIKE ?)"
                search_term = f"%{query}%"
                params.extend([search_term, search_term, search_term, search_term, search_term])

            if level:
                sql += " AND reading_level = ?"
                params.append(level)

            sql += " ORDER BY title"
            c.execute(sql, params)
            books = c.fetchall()
            conn.close()

            books_list = [_book_row_to_dict(book) for book in books]
            return jsonify({'books': books_list, 'count': len(books_list)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/loan', methods=['POST'])
    def api_bridge_books_loan():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('book_id')
        student_input = (payload.get('student_input') or '').strip()
        student_id = payload.get('student_id')

        if not book_id:
            return jsonify({'error': 'Book ID is required.'}), 400
        if not (student_input or student_id):
            return jsonify({'error': 'Student name or ID is required.'}), 400

        try:
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found.'}), 404
            if not (book[5] or book[6]):
                return jsonify({'error': 'Book cannot be loaned without an ISBN.'}), 400
            if (book[8] or 0) <= 0:
                return jsonify({'error': 'Book has zero copies and cannot be loaned.'}), 400
            if not book[3]:
                return jsonify({'error': 'Book is already loaned.'}), 400

            student_row = None
            if student_id:
                student_row = get_student(int(student_id))
            if not student_row and student_input:
                if student_input.isdigit():
                    student_row = get_student(int(student_input))
                if not student_row:
                    with sqlite3.connect(DB_PATH) as conn:
                        c = conn.cursor()
                        row = c.execute(
                            "SELECT id, name FROM students WHERE lower(name)=lower(?) LIMIT 1",
                            (student_input,)
                        ).fetchone()
                        if row:
                            student_row = (row[0], row[1])
            if not student_row:
                return jsonify({'error': 'Student not found.'}), 404

            resolved_student_id = student_row[0]
            checkout_date = loan_book(book_id, resolved_student_id)
            if not checkout_date:
                return jsonify({'error': 'Book or student not found for current user.'}), 404

            _invalidate_books_cache()
            return jsonify({
                'status': 'loaned',
                'book_id': book_id,
                'student_id': resolved_student_id,
                'student_name': student_row[1] if len(student_row) > 1 else None,
                'checkout_date': checkout_date,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/save', methods=['POST'])
    def api_bridge_books_save():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}

        title = (payload.get('title') or '').strip()
        author = (payload.get('author') or '').strip()
        publisher = (payload.get('publisher') or '').strip()
        isbn = _sanitize_isbn(payload.get('isbn')) if payload.get('isbn') else None
        isbn13 = _sanitize_isbn(payload.get('isbn13')) if payload.get('isbn13') else None
        level = (payload.get('reading_level') or '').strip()
        copies = _parse_non_negative_int(payload.get('copies'), default=1)
        existing_id = payload.get('id')

        if existing_id:
            try:
                update_book(
                    existing_id,
                    title=title or None,
                    author=author or None,
                    publisher=publisher or None,
                    isbn=isbn,
                    isbn13=isbn13,
                    reading_level=level or None,
                    copies=copies,
                    available=1 if (copies > 0 and (isbn or isbn13)) else 0,
                    borrower_id=payload.get('borrower_id') if payload.get('borrower_id') is not None else None
                )
                _invalidate_books_cache(existing_id)
                return jsonify({'status': 'updated', 'id': existing_id})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        incoming_title_key = _normalize_title_key(title)
        incoming_isbn_tokens = _isbn_tokens(isbn, isbn13)
        if incoming_title_key and incoming_isbn_tokens:
            isbn_match_row = None
            for token in incoming_isbn_tokens:
                isbn_match_row = find_book_by_isbn(token)
                if isbn_match_row:
                    break

            if isbn_match_row:
                existing_title_key = _normalize_title_key(isbn_match_row[1] if len(isbn_match_row) > 1 else '')
                existing_isbn_tokens = _isbn_tokens(
                    isbn_match_row[5] if len(isbn_match_row) > 5 else None,
                    isbn_match_row[6] if len(isbn_match_row) > 6 else None,
                )
                has_same_isbn = bool(existing_isbn_tokens.intersection(incoming_isbn_tokens))

                if existing_title_key == incoming_title_key and has_same_isbn:
                    existing_book_id = isbn_match_row[0]
                    existing_copies = _parse_non_negative_int(
                        isbn_match_row[8] if len(isbn_match_row) > 8 else 0,
                        default=0,
                    )
                    new_copies = existing_copies + max(1, copies)
                    update_book(existing_book_id, copies=new_copies)
                    _invalidate_books_cache(existing_book_id)
                    return jsonify({'status': 'updated', 'id': existing_book_id, 'new_copies': new_copies})

        if not title:
            return jsonify({'error': 'Title is required.'}), 400
        if not level:
            return jsonify({'error': 'Reading level is required for new books.'}), 400

        try:
            new_id = add_book(
                title=title,
                author=author,
                publisher=publisher,
                isbn=isbn,
                isbn13=isbn13,
                available=1 if (copies > 0 and (isbn or isbn13)) else 0,
                reading_level=level,
                copies=copies
            )
            _invalidate_books_cache(new_id)
            return jsonify({'status': 'created', 'id': new_id})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/increase_copies', methods=['POST'])
    def api_bridge_books_increase_copies():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('id')
        additional_copies = _parse_non_negative_int(payload.get('additional_copies'), default=1)

        if not book_id:
            return jsonify({'error': 'Book ID is required.'}), 400

        try:
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found.'}), 404

            current_copies = book[8] if len(book) > 8 else 1
            new_copies = current_copies + additional_copies
            update_book(book_id, copies=new_copies)
            _invalidate_books_cache()
            return jsonify({'status': 'updated', 'id': book_id, 'new_copies': new_copies})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/clear-loan', methods=['POST'])
    def api_bridge_books_clear_loan():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('book_id')
        student_id = payload.get('student_id')

        if not book_id or not student_id:
            return jsonify({'error': 'Book ID and Student ID are required.'}), 400

        try:
            book_id = int(book_id)
            student_id = int(student_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid book or student ID.'}), 400

        try:
            cleared_at = clear_active_loan(book_id, student_id)
            if not cleared_at:
                return jsonify({'error': 'No active loan found for this student and book.'}), 404
            _invalidate_books_cache()
            return jsonify({'status': 'cleared', 'book_id': book_id, 'student_id': student_id, 'cleared_at': cleared_at})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/return', methods=['POST'])
    def api_bridge_books_return():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('book_id')
        if not book_id:
            return jsonify({'error': 'Book ID is required.'}), 400
        try:
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found.'}), 404
            if book[3]:
                return jsonify({'error': 'Book is already available.'}), 400
            return_date = return_book(book_id)
            _invalidate_books_cache()
            return jsonify({'status': 'returned', 'book_id': book_id, 'return_date': return_date})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/<int:book_id>')
    def api_bridge_books_get(book_id: int):
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found'}), 404
            data = _book_row_to_dict(book)
            if data.get('borrower_id'):
                student = get_student(data['borrower_id'])
                if student:
                    data['borrower_name'] = student[1]
            return jsonify({'book': data})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/books/delete/<int:book_id>', methods=['POST'])
    def api_bridge_books_delete(book_id: int):
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            success = delete_book(book_id)
            if success:
                _invalidate_books_cache()
                return jsonify({'success': True, 'deleted': True})
            return jsonify({'success': False, 'deleted': False, 'error': 'Book not found'}), 404
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/bridge/students/suggest')
    def api_bridge_students_suggest():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()
        q = (request.args.get('q') or '').strip()
        if not q or len(q) < 3:
            return jsonify({'suggestions': []})

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            rows = c.execute(
                "SELECT id, name FROM students WHERE lower(name) LIKE lower(?) ORDER BY name LIMIT 10",
                (f"{q}%",)
            ).fetchall()
            suggestions = [{'id': row[0], 'name': row[1]} for row in rows]
            return jsonify({'suggestions': suggestions})

    @app.route('/api/bridge/students/active')
    def api_bridge_students_active():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            rows = c.execute(
                "SELECT id, name FROM students WHERE active = 1 ORDER BY name",
                (),
            ).fetchall()
            students = [{'id': row[0], 'name': row[1]} for row in rows]
            return jsonify({'students': students})

    @app.route('/api/bridge/students/lookup')
    def api_bridge_students_lookup():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()
        q = (request.args.get('q') or '').strip()
        if not q:
            return jsonify({'error': 'Student query is required.'}), 400
        student = None

        qr_match = re.match(r'^ID:(\d+)', q)
        if qr_match:
            student_id = int(qr_match.group(1))
            student = get_student(student_id)
            if student:
                return jsonify({'student': {'id': student[0], 'name': student[1]}})

        if q.isdigit():
            student = get_student(int(q))
            if student:
                return jsonify({'student': {'id': student[0], 'name': student[1]}})
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            row = c.execute(
                "SELECT id, name FROM students WHERE lower(name) = lower(?) LIMIT 1",
                (q,)
            ).fetchone()
            if row:
                return jsonify({'student': {'id': row[0], 'name': row[1]}})
        return jsonify({'error': 'Student not found.'}), 404
    
    # ----------------------------------------
    # Add Book page
    # ----------------------------------------
    @app.route("/books/add")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def books_add():
        return render_template("book_add.html", edit_book_id=None)

    @app.route("/books/edit/<int:book_id>")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def books_edit(book_id: int):
        book = get_book(book_id)
        if not book:
            flash("Book not found", "danger")
            return redirect(url_for("books_list"))
        return render_template("book_add.html", edit_book_id=book_id)
    
    @app.route("/books")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def books_list():
        """Display all books in the library."""
        try:
            # Normalize DB: any book without ISBN should be unavailable
            enforce_isbn_availability_rule()
            books = get_books()
            
            # Convert to list of dicts for easier template rendering
            books_list = []
            for book in books:
                borrower_id = book[9] if len(book) > 9 else None
                borrower_name = None
                if borrower_id:
                    student = get_student(borrower_id)
                    if student:
                        borrower_name = student[1] if len(student) > 1 else None
                
                # Book is available only if it has ISBN AND no borrower
                has_isbn = (book[5] if len(book) > 5 else None) or (book[6] if len(book) > 6 else None)
                copies = (book[8] if len(book) > 8 else 0) or 0
                is_available = 1 if (copies > 0 and has_isbn and not borrower_id) else 0
                
                books_list.append({
                    'id': book[0],
                    'title': book[1],
                    'author': book[2],
                    'available': is_available,
                    'reading_level': book[4] if len(book) > 4 else None,
                    'isbn': book[5] if len(book) > 5 else None,
                    'isbn13': book[6] if len(book) > 6 else None,
                    'publisher': book[7] if len(book) > 7 else None,
                    'copies': book[8] if len(book) > 8 else 1,
                    'borrower_id': borrower_id,
                    'borrower_name': borrower_name,
                })
            
            return render_template(
                "books_list.html",
                books=books_list,
                total_books=len(books_list)
            )
        except Exception as e:
            flash(f"Error loading books: {str(e)}", "danger")
            return render_template("books_list.html", books=[], total_books=0)
    
    @app.route("/api/books/catalog")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_catalog():
        """Return full book catalog details with slower cache lane."""
        forwarded = _bridge_forward_get('/api/bridge/books/catalog')
        if forwarded is not None:
            return forwarded

        def _build_catalog_payload():
            enforce_isbn_availability_rule()
            rows = get_books()
            books_payload = [_book_row_to_dict(row) for row in rows]
            return {'books': books_payload, 'count': len(books_payload)}

        payload = server_cache.get_or_set(
            _books_catalog_cache_key(),
            _build_catalog_payload,
            policy="book_catalog",
        )
        return jsonify(payload)

    @app.route("/api/books/search")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_search():
        """API endpoint to search/filter books."""
        forwarded = _bridge_forward_get('/api/bridge/books/search', request.args.to_dict(flat=True))
        if forwarded is not None:
            return forwarded

        query_raw = request.args.get('q', '').strip()
        query = query_raw.lower()
        level = request.args.get('level', '').strip()
        isbn_candidates = _expand_isbn_candidates(query_raw)
        
        try:
            # Normalize DB before search
            enforce_isbn_availability_rule()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            
            # Build dynamic query
            sql = "SELECT id, title, author, available, reading_level, isbn, isbn13, publisher, copies, borrower_id FROM books WHERE 1=1"
            params = []

            # For scanner-friendly ISBN searches, prefer exact normalized ISBN matches
            # (including ISBN-10/13 equivalent conversion) over broad LIKE matching.
            if isbn_candidates:
                placeholders = ",".join(["?" for _ in isbn_candidates])
                sql += (
                    f" AND (UPPER(TRIM(COALESCE(isbn, ''))) IN ({placeholders}) "
                    f"OR UPPER(TRIM(COALESCE(isbn13, ''))) IN ({placeholders}))"
                )
                params.extend(isbn_candidates)
                params.extend(isbn_candidates)
            elif query:
                sql += " AND (title LIKE ? OR author LIKE ? OR publisher LIKE ? OR isbn LIKE ? OR isbn13 LIKE ?)"
                search_term = f"%{query}%"
                params.extend([search_term, search_term, search_term, search_term, search_term])
            
            if level:
                sql += " AND reading_level = ?"
                params.append(level)
            
            sql += " ORDER BY title"
            
            c.execute(sql, params)
            books = c.fetchall()
            conn.close()
            
            books_list = []
            for book in books:
                # Use helper to include borrower_name and derived fields
                data = _book_row_to_dict(book)
                books_list.append(data)
            
            return jsonify({'books': books_list, 'count': len(books_list)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route("/api/books/levels")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_levels():
        """Get all unique reading levels."""
        forwarded = _bridge_forward_get('/api/bridge/books/levels')
        if forwarded is not None:
            return forwarded

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT DISTINCT reading_level FROM books WHERE reading_level IS NOT NULL")
            raw_levels = [str(row[0]).strip() for row in c.fetchall() if str(row[0] or '').strip()]
            conn.close()

            level_rank = {level: idx for idx, level in enumerate(BOOK_LEVEL_ORDER)}
            normalized = []
            seen = set()
            for level in raw_levels:
                if level in seen:
                    continue
                seen.add(level)
                normalized.append(level)

            levels = sorted(
                normalized,
                key=lambda lv: (level_rank.get(lv, len(BOOK_LEVEL_ORDER)), lv)
            )
            return jsonify({'levels': levels})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route("/books/loan")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def books_loan_page():
        from datetime import datetime
        loaned_books = get_loaned_books_detailed()
        
        # Group books by student for display
        books_by_student = {}
        for row in loaned_books:
            student_name = row['student_name']
            book_title = row['book_title']
            checkout_date = row['checkout_date']
            student_id = row['student_id']
            book_id = row['book_id']
            loan_id = row['loan_id']

            if student_name not in books_by_student:
                books_by_student[student_name] = []
            
            # Format the checkout date
            try:
                date_obj = datetime.fromisoformat(checkout_date.replace('Z', '+00:00'))
                formatted_date = date_obj.strftime('%Y-%m-%d')
            except:
                formatted_date = checkout_date[:10] if len(checkout_date) >= 10 else checkout_date
            
            books_by_student[student_name].append({
                'title': book_title,
                'checkout_date': formatted_date,
                'student_id': student_id,
                'book_id': book_id,
                'loan_id': loan_id,
                'show_clear': bool(row.get('show_clear')),
            })
        
        return render_template(
            "book_loan.html",
            books_by_student=books_by_student,
            total_loans=len(loaned_books)
        )

    @app.route("/api/students/suggest")
    @require_login
    def api_students_suggest():
        """Return student name suggestions for autocomplete."""
        forwarded = _bridge_forward_get('/api/bridge/students/suggest', request.args.to_dict(flat=True))
        if forwarded is not None:
            return forwarded

        q = (request.args.get('q') or '').strip()
        if not q or len(q) < 3:
            return jsonify({'suggestions': []})
        
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            rows = c.execute(
                "SELECT id, name FROM students WHERE lower(name) LIKE lower(?) ORDER BY name LIMIT 10",
                (f"{q}%",)
            ).fetchall()
            suggestions = [{'id': row[0], 'name': row[1]} for row in rows]
            return jsonify({'suggestions': suggestions})

    @app.route("/api/students/active")
    @require_login
    def api_students_active():
        """Return all active students ordered by name for dropdown selection."""
        forwarded = _bridge_forward_get('/api/bridge/students/active', request.args.to_dict(flat=True))
        if forwarded is not None:
            return forwarded

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            rows = c.execute(
                "SELECT id, name FROM students WHERE active = 1 ORDER BY name",
                (),
            ).fetchall()
            students = [{'id': row[0], 'name': row[1]} for row in rows]
            return jsonify({'students': students})

    @app.route("/api/students/lookup")
    @require_login
    def api_students_lookup():
        """Lookup a student by id (numeric) or exact name (case-insensitive)."""
        forwarded = _bridge_forward_get('/api/bridge/students/lookup', request.args.to_dict(flat=True))
        if forwarded is not None:
            return forwarded

        q = (request.args.get('q') or '').strip()
        if not q:
            return jsonify({'error': 'Student query is required.'}), 400
        student = None
        
        # Parse QR code format: "ID:4Name:Aahan A." or "ID:4\nName:Aahan A."
        import re
        qr_match = re.match(r'^ID:(\d+)', q)
        if qr_match:
            student_id = int(qr_match.group(1))
            student = get_student(student_id)
            if student:
                return jsonify({'student': {'id': student[0], 'name': student[1]}})
        
        if q.isdigit():
            student = get_student(int(q))
            if student:
                return jsonify({'student': {'id': student[0], 'name': student[1]}})
        # name lookup
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            row = c.execute(
                "SELECT id, name FROM students WHERE lower(name) = lower(?) LIMIT 1",
                (q,)
            ).fetchone()
            if row:
                return jsonify({'student': {'id': row[0], 'name': row[1]}})
        return jsonify({'error': 'Student not found.'}), 404

    # ----------------------------------------
    # ISBN lookup (fetch details from Library of Congress JSON)
    # ----------------------------------------
    @app.route("/api/books/isbn_lookup")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_isbn_lookup():
        forwarded = _bridge_forward_get('/api/bridge/books/isbn_lookup', request.args.to_dict(flat=True))
        if forwarded is not None:
            return forwarded

        isbn_raw = (request.args.get('isbn') or '').strip()
        isbn = _sanitize_isbn(isbn_raw)

        if not isbn or len(isbn) not in (10, 13):
            return jsonify({'error': 'Please scan or enter a valid ISBN-10 or ISBN-13.'}), 400

        cooldown = server_cache.get_cache(ISBN_LOOKUP_RATE_LIMIT_COOLDOWN_KEY)
        if cooldown:
            retry_after = int(cooldown.get('retry_after_seconds', 5)) if isinstance(cooldown, dict) else 5
            return jsonify({
                'error': 'Google Books is rate-limiting requests. Please retry shortly.',
                'retry_after_seconds': retry_after,
            }), 429

        try:
            data = _lookup_isbn_online_cached(isbn)
        except IsbnLookupError as e:
            if e.status_code == 429:
                retry_after = max(1, e.retry_after_seconds or 5)
                server_cache.set_cache(
                    ISBN_LOOKUP_RATE_LIMIT_COOLDOWN_KEY,
                    {'retry_after_seconds': retry_after},
                    ttl_seconds=retry_after,
                )
                return jsonify({
                    'error': 'Google Books is rate-limiting requests. Please retry shortly.',
                    'retry_after_seconds': retry_after,
                }), 429
            return jsonify({'error': f"Lookup failed: {e}"}), 502
        except Exception as e:
            return jsonify({'error': f"Lookup failed: {e}"}), 502

        # Check for existing book by ISBN
        existing_by_isbn = find_book_by_isbn(isbn)
        isbn_existing_id = existing_by_isbn[0] if existing_by_isbn else None
        isbn_existing_book = None
        if isbn_existing_id:
            isbn_existing_book = get_book(isbn_existing_id)

        # Check for existing book by title
        existing = find_book_by_title(data.get('title')) if data.get('title') else None
        existing_id = existing[0] if existing else None
        existing_book = None
        if existing_id:
            existing_book = get_book(existing_id)

        return jsonify({
            'book': data,
            'existing_id': existing_id,
            'existing_book': _book_row_to_dict(existing_book) if existing_book else None,
            'isbn_existing_id': isbn_existing_id,
            'isbn_existing_book': _book_row_to_dict(isbn_existing_book) if isbn_existing_book else None,
            'message': "Book found" if data else "No data found",
        })

    # ----------------------------------------
    # Save / upsert book
    # ----------------------------------------
    @app.route("/api/books/save", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_save():
        forwarded = _bridge_forward_post('/api/bridge/books/save', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}

        title = (payload.get('title') or '').strip()
        author = (payload.get('author') or '').strip()
        publisher = (payload.get('publisher') or '').strip()
        isbn = _sanitize_isbn(payload.get('isbn')) if payload.get('isbn') else None
        isbn13 = _sanitize_isbn(payload.get('isbn13')) if payload.get('isbn13') else None
        level = (payload.get('reading_level') or '').strip()
        copies = _parse_non_negative_int(payload.get('copies'), default=1)
        existing_id = payload.get('id')

        # Existing book: update provided fields
        if existing_id:
            try:
                update_book(
                    existing_id,
                    title=title or None,
                    author=author or None,
                    publisher=publisher or None,
                    isbn=isbn,
                    isbn13=isbn13,
                    reading_level=level or None,
                    copies=copies,
                    # Availability is derived from ISBN + copies + borrower state.
                    available=1 if (copies > 0 and (isbn or isbn13)) else 0,
                    borrower_id=payload.get('borrower_id') if payload.get('borrower_id') is not None else None
                )
                _invalidate_books_cache(existing_id)
                return jsonify({'status': 'updated', 'id': existing_id})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        # Add flow (no explicit id): merge copies only when BOTH title and ISBN match.
        incoming_title_key = _normalize_title_key(title)
        incoming_isbn_tokens = _isbn_tokens(isbn, isbn13)
        if incoming_title_key and incoming_isbn_tokens:
            isbn_match_row = None
            for token in incoming_isbn_tokens:
                isbn_match_row = find_book_by_isbn(token)
                if isbn_match_row:
                    break

            if isbn_match_row:
                existing_title_key = _normalize_title_key(isbn_match_row[1] if len(isbn_match_row) > 1 else '')
                existing_isbn_tokens = _isbn_tokens(
                    isbn_match_row[5] if len(isbn_match_row) > 5 else None,
                    isbn_match_row[6] if len(isbn_match_row) > 6 else None,
                )
                has_same_isbn = bool(existing_isbn_tokens.intersection(incoming_isbn_tokens))

                if existing_title_key == incoming_title_key and has_same_isbn:
                    existing_book_id = isbn_match_row[0]
                    existing_copies = _parse_non_negative_int(
                        isbn_match_row[8] if len(isbn_match_row) > 8 else 0,
                        default=0,
                    )
                    new_copies = existing_copies + max(1, copies)
                    update_book(existing_book_id, copies=new_copies)
                    _invalidate_books_cache(existing_book_id)
                    return jsonify({'status': 'updated', 'id': existing_book_id, 'new_copies': new_copies})

        # New book: require title and level
        if not title:
            return jsonify({'error': 'Title is required.'}), 400
        if not level:
            return jsonify({'error': 'Reading level is required for new books.'}), 400

        try:
            new_id = add_book(
                title=title,
                author=author,
                publisher=publisher,
                isbn=isbn,
                isbn13=isbn13,
                # New books must have copies + ISBN to be loanable.
                available=1 if (copies > 0 and (isbn or isbn13)) else 0,
                reading_level=level,
                copies=copies
            )
            _invalidate_books_cache(new_id)
            return jsonify({'status': 'created', 'id': new_id})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ----------------------------------------
    # Increase book copies
    # ----------------------------------------
    @app.route("/api/books/increase_copies", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_increase_copies():
        """Increase the number of copies for an existing book."""
        forwarded = _bridge_forward_post('/api/bridge/books/increase_copies', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('id')
        additional_copies = _parse_non_negative_int(payload.get('additional_copies'), default=1)

        if not book_id:
            return jsonify({'error': 'Book ID is required.'}), 400

        try:
            # Get current book
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found.'}), 404

            current_copies = book[8] if len(book) > 8 else 1
            new_copies = current_copies + additional_copies

            # Update copies count
            update_book(book_id, copies=new_copies)
            _invalidate_books_cache()
            return jsonify({'status': 'updated', 'id': book_id, 'new_copies': new_copies})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ----------------------------------------
    # Loan / Return endpoints
    # ----------------------------------------
    @app.route("/api/books/loan", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_loan():
        forwarded = _bridge_forward_post('/api/bridge/books/loan', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('book_id')
        student_input = (payload.get('student_input') or '').strip()
        student_id = payload.get('student_id')

        if not book_id:
            return jsonify({'error': 'Book ID is required.'}), 400
        if not (student_input or student_id):
            return jsonify({'error': 'Student name or ID is required.'}), 400

        try:
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found.'}), 404
            # Disallow loaning books without any ISBN
            if not (book[5] or book[6]):
                return jsonify({'error': 'Book cannot be loaned without an ISBN.'}), 400
            if (book[8] or 0) <= 0:
                return jsonify({'error': 'Book has zero copies and cannot be loaned.'}), 400
            if not book[3]:
                return jsonify({'error': 'Book is already loaned.'}), 400

            # Resolve student by explicit id, by numeric QR, or by name
            student_row = None
            if student_id:
                student_row = get_student(int(student_id))
            if not student_row and student_input:
                if student_input.isdigit():
                    student_row = get_student(int(student_input))
                if not student_row:
                    with sqlite3.connect(DB_PATH) as conn:
                        c = conn.cursor()
                        row = c.execute(
                            "SELECT id, name FROM students WHERE lower(name)=lower(?) LIMIT 1",
                            (student_input,)
                        ).fetchone()
                        if row:
                            student_row = (row[0], row[1])
            if not student_row:
                return jsonify({'error': 'Student not found.'}), 404

            student_id = student_row[0]
            checkout_date = loan_book(book_id, student_id)
            if not checkout_date:
                return jsonify({'error': 'Book or student not found for current user.'}), 404

            try:
                pushed = sync_to_gdrive_now()
                if not pushed:
                    print("[sync] WARNING: immediate cloud push after book loan was skipped/failed.")
            except Exception as push_exc:
                print(f"[sync] WARNING: immediate cloud push after book loan error: {push_exc}")

            _invalidate_books_cache()
            if _scanner_api_client_enabled() and bool(getattr(g, 'scanner_bridge_fallback', False)):
                scanner_sync.enqueue_mutation(
                    'book_loan',
                    {
                        'book_id': int(book_id),
                        'student_id': int(student_id),
                    },
                )
            return jsonify({
                'status': 'loaned',
                'book_id': book_id,
                'student_id': student_id,
                'student_name': student_row[1] if len(student_row) > 1 else None,
                'checkout_date': checkout_date,
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route("/api/books/clear-loan", methods=["POST"])
    @require_admin
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_clear_loan():
        """Clear an active loan directly from the loaned books table."""
        forwarded = _bridge_forward_post('/api/bridge/books/clear-loan', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('book_id')
        student_id = payload.get('student_id')

        if not book_id or not student_id:
            return jsonify({'error': 'Book ID and Student ID are required.'}), 400

        try:
            book_id = int(book_id)
            student_id = int(student_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid book or student ID.'}), 400

        try:
            cleared_at = clear_active_loan(book_id, student_id)
            if not cleared_at:
                return jsonify({'error': 'No active loan found for this student and book.'}), 404
            _invalidate_books_cache()
            if _scanner_api_client_enabled() and bool(getattr(g, 'scanner_bridge_fallback', False)):
                scanner_sync.enqueue_mutation(
                    'book_clear_loan',
                    {
                        'book_id': int(book_id),
                        'student_id': int(student_id),
                    },
                )
            return jsonify({'status': 'cleared', 'book_id': book_id, 'student_id': student_id, 'cleared_at': cleared_at})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route("/api/books/return", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_return():
        forwarded = _bridge_forward_post('/api/bridge/books/return', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        book_id = payload.get('book_id')
        if not book_id:
            return jsonify({'error': 'Book ID is required.'}), 400
        try:
            book = get_book(book_id)
            if not book:
                return jsonify({'error': 'Book not found.'}), 404
            if book[3]:
                return jsonify({'error': 'Book is already available.'}), 400
            return_date = return_book(book_id)
            _invalidate_books_cache()
            if _scanner_api_client_enabled() and bool(getattr(g, 'scanner_bridge_fallback', False)):
                scanner_sync.enqueue_mutation(
                    'book_return',
                    {
                        'book_id': int(book_id),
                    },
                )
            return jsonify({'status': 'returned', 'book_id': book_id, 'return_date': return_date})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ----------------------------------------
    # Sync all students' book_loaned status
    # ----------------------------------------
    @app.route("/api/books/sync-student-status", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_sync_student_status():
        """Sync all students' book_loaned flag based on current book loans."""
        backup_path = db_backup_recovery.create_backup("books_sync_student_status")
        cache_invalidators = (
            lambda: _invalidate_book_sync_caches(),
        )
        try:
            count = sync_all_students_book_status()
            invalidate_scoped_cache(*cache_invalidators)
            return jsonify({'status': 'success', 'students_updated': count, 'backup': backup_path})
        except Exception as e:
            return json_scoped_failure(
                backup_path=backup_path,
                table_names=("students", "books"),
                error=e,
                invalidators=cache_invalidators,
            )

    # ----------------------------------------
    # Get single book details (for editing)
    # ----------------------------------------
    @app.route("/api/books/<int:book_id>")
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_books_get(book_id: int):
        forwarded = _bridge_forward_get(f'/api/bridge/books/{book_id}')
        if forwarded is not None:
            return forwarded

        try:
            cache_key = _book_detail_cache_key(book_id)
            def _build_book_detail_payload():
                book = get_book(book_id)
                if not book:
                    return None
                data = _book_row_to_dict(book)
                if data.get('borrower_id'):
                    student = get_student(data['borrower_id'])
                    if student:
                        data['borrower_name'] = student[1]
                return {'book': data}

            payload = server_cache.get_or_set(
                cache_key,
                _build_book_detail_payload,
                policy="book_catalog",
            )
            if payload is None:
                return jsonify({'error': 'Book not found'}), 404
            return jsonify(payload)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ----------------------------------------
    # Delete book
    # ----------------------------------------
    @app.route("/books/delete/<int:book_id>", methods=["POST"])
    @require_admin
    @require_feature(auth_manager.FEATURE_BOOKS)
    def books_delete(book_id: int):
        if _scanner_api_client_enabled():
            forwarded = _bridge_forward_post(f'/api/bridge/books/delete/{book_id}', {})
            if forwarded is not None:
                response, status_code = forwarded
                payload = response.get_json(silent=True) or {}
                if 200 <= int(status_code) < 300 and payload.get('deleted'):
                    flash("Book deleted", "success")
                else:
                    flash(payload.get('error') or 'Delete failed on Instructor Station.', 'danger')
                return redirect(url_for("books_list"))

        try:
            success = delete_book(book_id)
            if success:
                _invalidate_books_cache()
                flash("Book deleted", "success")
            else:
                flash("Book not found", "warning")
        except Exception as e:
            flash(f"Delete failed: {e}", "danger")
        return redirect(url_for("books_list"))


# ================================================
# Helper functions
# ================================================
def _sanitize_isbn(value: str):
    if not value:
        return None
    digits = re.sub(r"[^0-9Xx]", "", value)
    return digits.upper()


def _isbn10_to_isbn13(isbn10: str | None) -> str | None:
    """Convert ISBN-10 to ISBN-13 (978 prefix) when input shape is valid."""
    token = _sanitize_isbn(isbn10)
    if not token or len(token) != 10:
        return None

    core = token[:9]
    if not core.isdigit():
        return None

    body = f"978{core}"
    total = 0
    for idx, ch in enumerate(body):
        n = int(ch)
        total += n if idx % 2 == 0 else n * 3
    check_digit = (10 - (total % 10)) % 10
    return f"{body}{check_digit}"


def _isbn13_to_isbn10(isbn13: str | None) -> str | None:
    """Convert ISBN-13 to ISBN-10 when the number starts with 978."""
    token = _sanitize_isbn(isbn13)
    if not token or len(token) != 13 or not token.isdigit() or not token.startswith("978"):
        return None

    core9 = token[3:12]
    total = 0
    for idx, ch in enumerate(core9, start=1):
        total += idx * int(ch)
    remainder = total % 11
    check_digit = "X" if remainder == 10 else str(remainder)
    return f"{core9}{check_digit}"


def _expand_isbn_candidates(raw_value: str) -> list[str]:
    """Return normalized ISBN candidates including 10/13 converted equivalents."""
    token = _sanitize_isbn(raw_value)
    if not token or len(token) not in (10, 13):
        return []

    candidates = [token]
    if len(token) == 10:
        converted = _isbn10_to_isbn13(token)
        if converted:
            candidates.append(converted)
    else:
        converted = _isbn13_to_isbn10(token)
        if converted:
            candidates.append(converted)

    # Stable de-duplication, preserving input token priority.
    ordered_unique: list[str] = []
    seen = set()
    for candidate in candidates:
        normalized = str(candidate or '').strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered_unique.append(normalized)
    return ordered_unique


def _lookup_isbn_online(isbn: str):
    """Fetch book details using provider chain: Google Books -> Open Library fallback."""
    try:
        return _lookup_isbn_google(isbn)
    except IsbnLookupError as google_err:
        should_fallback = google_err.status_code in (404, 429, 500, 502, 503, 504)
        if not should_fallback:
            raise

        try:
            return _lookup_isbn_open_library(isbn)
        except IsbnLookupError as openlib_err:
            retry_after_seconds = google_err.retry_after_seconds if google_err.status_code == 429 else 0
            raise IsbnLookupError(
                f"Lookup failed (Google: {google_err}; Open Library: {openlib_err})",
                status_code=502,
                retry_after_seconds=retry_after_seconds,
            )


def _lookup_isbn_google(isbn: str):
    """Fetch book details from Google Books API using ISBN."""
    url = "https://www.googleapis.com/books/v1/volumes"
    params = {
        "q": f"isbn:{isbn}",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Stdytime/1.0)",
    }

    max_attempts = 4
    backoff_seconds = [0.4, 0.9, 1.8]
    resp = None
    last_retry_after = 0

    for attempt in range(max_attempts):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
        except requests.RequestException as exc:
            raise IsbnLookupError("Google Books request failed", status_code=502) from exc
        if resp.status_code == 200:
            break

        if resp.status_code == 429:
            retry_after_header = str(resp.headers.get("Retry-After", "")).strip()
            retry_after = 0
            if retry_after_header.isdigit():
                retry_after = max(0, int(retry_after_header))

            backoff = backoff_seconds[min(attempt, len(backoff_seconds) - 1)]
            sleep_seconds = min(8.0, max(float(retry_after), backoff))
            last_retry_after = max(last_retry_after, int(round(sleep_seconds)))

            if attempt < (max_attempts - 1):
                time.sleep(sleep_seconds)
                continue

            raise IsbnLookupError(
                "Google Books API returned 429",
                status_code=429,
                retry_after_seconds=max(1, last_retry_after),
            )

        if resp.status_code in (500, 502, 503, 504) and attempt < (max_attempts - 1):
            time.sleep(backoff_seconds[min(attempt, len(backoff_seconds) - 1)])
            continue

        raise IsbnLookupError(f"Google Books API returned {resp.status_code}", status_code=resp.status_code)

    if resp is None or resp.status_code != 200:
        status_code = resp.status_code if resp is not None else 502
        raise IsbnLookupError(f"Google Books API returned {status_code}", status_code=status_code)

    try:
        payload = resp.json()
    except Exception as exc:
        raise IsbnLookupError("Invalid JSON response from Google Books API") from exc

    items = payload.get("items") or []
    if not items:
        raise IsbnLookupError("No results found on Google Books for this ISBN", status_code=404)

    first_book = items[0] or {}
    volume_info = first_book.get("volumeInfo") or {}

    # Extract fields with safe fallbacks
    title = volume_info.get("title") or ""
    
    # Author may be a list; take first one
    authors = volume_info.get("authors") or []
    author = authors[0] if authors else ""
    
    # Publisher
    publisher = volume_info.get("publisher") or ""
    
    # ISBN info
    isbn_data = volume_info.get("industryIdentifiers") or []
    isbn10 = None
    isbn13 = None
    for id_entry in isbn_data:
        if id_entry.get("type") == "ISBN_10":
            isbn10 = id_entry.get("identifier")
        elif id_entry.get("type") == "ISBN_13":
            isbn13 = id_entry.get("identifier")
    
    # Fallback: use input ISBN if not found in response
    if not isbn10 and not isbn13:
        if len(isbn) == 10:
            isbn10 = isbn
        elif len(isbn) == 13:
            isbn13 = isbn

    if not title:
        raise IsbnLookupError("No book title found in Google Books response")

    return {
        'title': title.strip(),
        'author': author.strip(),
        'publisher': publisher.strip(),
        'isbn': isbn10,
        'isbn13': isbn13,
    }


def _lookup_isbn_open_library(isbn: str):
    """Fallback lookup via Open Library (ISBN API first, then search API)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Stdytime/1.0)",
    }

    details_url = "https://openlibrary.org/api/books"
    details_params = {
        "bibkeys": f"ISBN:{isbn}",
        "format": "json",
        "jscmd": "data",
    }

    try:
        resp = requests.get(details_url, params=details_params, headers=headers, timeout=10)
    except requests.RequestException as exc:
        raise IsbnLookupError("Open Library request failed", status_code=502) from exc

    if resp.status_code not in (200, 404):
        raise IsbnLookupError(f"Open Library returned {resp.status_code}", status_code=resp.status_code)

    payload = {}
    if resp.status_code == 200:
        try:
            payload = resp.json() or {}
        except Exception as exc:
            raise IsbnLookupError("Invalid JSON response from Open Library") from exc

    item = payload.get(f"ISBN:{isbn}") if isinstance(payload, dict) else None
    if item:
        title = str(item.get("title") or "").strip()
        authors = item.get("authors") or []
        publishers = item.get("publishers") or []
        identifiers = item.get("identifiers") or {}

        author = ""
        if isinstance(authors, list) and authors:
            first_author = authors[0]
            if isinstance(first_author, dict):
                author = str(first_author.get("name") or "").strip()
            else:
                author = str(first_author or "").strip()

        publisher = ""
        if isinstance(publishers, list) and publishers:
            first_publisher = publishers[0]
            if isinstance(first_publisher, dict):
                publisher = str(first_publisher.get("name") or "").strip()
            else:
                publisher = str(first_publisher or "").strip()

        isbn10 = None
        isbn13 = None
        if isinstance(identifiers, dict):
            isbn10_list = identifiers.get("isbn_10") or []
            isbn13_list = identifiers.get("isbn_13") or []
            isbn10 = str(isbn10_list[0]).strip() if isbn10_list else None
            isbn13 = str(isbn13_list[0]).strip() if isbn13_list else None

        if not isbn10 and not isbn13:
            if len(isbn) == 10:
                isbn10 = isbn
            elif len(isbn) == 13:
                isbn13 = isbn

        if title:
            return {
                'title': title,
                'author': author,
                'publisher': publisher,
                'isbn': isbn10,
                'isbn13': isbn13,
            }

    # Secondary fallback: Open Library search endpoint
    search_url = "https://openlibrary.org/search.json"
    search_params = {"isbn": isbn}
    try:
        search_resp = requests.get(search_url, params=search_params, headers=headers, timeout=10)
    except requests.RequestException as exc:
        raise IsbnLookupError("Open Library search request failed", status_code=502) from exc

    if search_resp.status_code != 200:
        raise IsbnLookupError(f"Open Library search returned {search_resp.status_code}", status_code=search_resp.status_code)

    try:
        search_payload = search_resp.json() or {}
    except Exception as exc:
        raise IsbnLookupError("Invalid JSON response from Open Library search") from exc

    docs = search_payload.get("docs") or []
    if not docs:
        raise IsbnLookupError("No results found on Open Library for this ISBN", status_code=404)

    first = docs[0] or {}
    title = str(first.get("title") or "").strip()
    if not title:
        raise IsbnLookupError("No book title found in Open Library response")

    authors = first.get("author_name") or []
    publishers = first.get("publisher") or []
    isbn_values = [str(v).strip().upper() for v in (first.get("isbn") or []) if str(v).strip()]
    isbn10 = next((v for v in isbn_values if len(v) == 10), None)
    isbn13 = next((v for v in isbn_values if len(v) == 13), None)

    if not isbn10 and not isbn13:
        if len(isbn) == 10:
            isbn10 = isbn
        elif len(isbn) == 13:
            isbn13 = isbn

    return {
        'title': title,
        'author': str(authors[0]).strip() if authors else "",
        'publisher': str(publishers[0]).strip() if publishers else "",
        'isbn': isbn10,
        'isbn13': isbn13,
    }


def _first_text(items):
    return items[0].strip() if items else None


def _normalize_title_key(value: str) -> str:
    return str(value or '').strip().lower()


def _isbn_tokens(isbn: str, isbn13: str) -> set[str]:
    tokens = set()
    for token in (isbn, isbn13):
        clean = _sanitize_isbn(token) if token else None
        if clean:
            tokens.add(clean)
    return tokens


def _book_row_to_dict(row):
    if not row:
        return None
    borrower_id = row[9]
    borrower_name = None
    if borrower_id:
        student = get_student(borrower_id)
        if student:
            borrower_name = student[1] if len(student) > 1 else None
    
    # Book is available only if it has ISBN AND no borrower
    has_isbn = row[5] or row[6]
    copies = row[8] or 0
    is_available = 1 if (copies > 0 and has_isbn and not borrower_id) else 0
    
    return {
        'id': row[0],
        'title': row[1],
        'author': row[2],
        'available': is_available,
        'reading_level': row[4],
        'isbn': row[5],
        'isbn13': row[6],
        'publisher': row[7],
        'copies': row[8],
        'borrower_id': borrower_id,
        'borrower_name': borrower_name,
    }
