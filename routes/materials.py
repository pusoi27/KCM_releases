"""Devices management routes (mirrors Books/Devices with QR-based identity)."""

import re
import sqlite3
import requests

from flask import flash, jsonify, redirect, render_template, request, url_for, g

from modules import auth_manager, db_backup_recovery, server_cache
from modules import scanner_sync
from modules.database import DB_PATH, sync_to_gdrive_now, get_station_runtime_config
from modules.database import qr_token_exists
from modules.materials_manager import (
    add_material,
    clear_active_material_loan,
    delete_material,
    enforce_qr_availability_rule,
    find_material_by_qr_code,
    find_material_by_title,
    get_loaned_materials_detailed,
    get_material,
    get_materials,
    loan_material,
    return_material,
    sync_all_students_material_status,
    update_material,
)
from modules.student_manager import get_student
from routes.auth import require_admin, require_feature, require_login
from routes.operation_utils import invalidate_scoped_cache, json_scoped_failure

BRIDGE_GET_TIMEOUT_SECONDS = 4
BRIDGE_POST_TIMEOUT_SECONDS = 5


def _materials_catalog_cache_key() -> str:
    return "materials:catalog:v1"


def _material_detail_cache_key(material_id) -> str:
    return f"materials:detail:v1:{material_id}"


def _material_detail_prefix() -> str:
    return "materials:detail:v1:"


def _invalidate_materials_cache(material_id=None):
    server_cache.invalidate(_materials_catalog_cache_key())
    if material_id is not None:
        try:
            server_cache.invalidate(_material_detail_cache_key(material_id))
        except (TypeError, ValueError):
            pass


def _invalidate_material_sync_caches():
    server_cache.invalidate(_materials_catalog_cache_key())
    server_cache.invalidate_prefix(_material_detail_prefix())


def _parse_non_negative_int(value, default=0):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(parsed, 0)


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


def _normalize_qr_value(value: str | None) -> str | None:
    raw = (value or '').strip()
    if not raw:
        return None

    # Accept scanner payloads like "MAT:12\nTitle:...\nCode:MAT-000012-ABC123"
    code_match = re.search(r"Code:([^\n\r]+)", raw, flags=re.IGNORECASE)
    if code_match:
        return code_match.group(1).strip().upper()

    mat_line = re.search(r"^MAT:(\d+)", raw, flags=re.IGNORECASE)
    if mat_line:
        return f"MAT-{int(mat_line.group(1)):06d}"

    return raw.upper()


def _material_row_to_dict(row):
    if not row:
        return None
    borrower_id = row[8]
    borrower_name = None
    if borrower_id:
        student = get_student(borrower_id)
        if student:
            borrower_name = student[1] if len(student) > 1 else None

    has_qr = bool(row[5])
    copies = row[7] or 0
    is_available = 1 if (copies > 0 and has_qr and not borrower_id) else 0

    return {
        'id': row[0],
        'title': row[1],
        'author': row[2],
        'available': is_available,
        'reading_level': row[4],
        'qr_code': row[5],
        'publisher': row[6],
        'copies': row[7],
        'borrower_id': borrower_id,
        'borrower_name': borrower_name,
    }


def register_material_routes(app):
    @app.route('/api/bridge/materials/catalog')
    def api_bridge_materials_catalog():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            enforce_qr_availability_rule()
            rows = get_materials()
            materials_payload = [_material_row_to_dict(row) for row in rows]
            return jsonify({'materials': materials_payload, 'count': len(materials_payload)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/levels')
    def api_bridge_materials_levels():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                c = conn.cursor()
                c.execute('SELECT DISTINCT reading_level FROM materials WHERE reading_level IS NOT NULL ORDER BY reading_level')
                levels = [row[0] for row in c.fetchall()]
            return jsonify({'levels': levels})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/search')
    def api_bridge_materials_search():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        query = request.args.get('q', '').strip().lower()
        level = request.args.get('level', '').strip()

        try:
            enforce_qr_availability_rule()
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                c = conn.cursor()

                sql = "SELECT id, title, author, available, reading_level, qr_code, publisher, copies, borrower_id FROM materials WHERE 1=1"
                params = []

                if query:
                    sql += " AND (title LIKE ? OR author LIKE ? OR publisher LIKE ? OR qr_code LIKE ?)"
                    search_term = f"%{query}%"
                    params.extend([search_term, search_term, search_term, search_term])

                if level:
                    sql += " AND reading_level = ?"
                    params.append(level)

                sql += " ORDER BY title"
                c.execute(sql, params)
                rows = c.fetchall()

            return jsonify({'materials': [_material_row_to_dict(row) for row in rows], 'count': len(rows)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/loan', methods=['POST'])
    def api_bridge_materials_loan():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('material_id')
        student_input = (payload.get('student_input') or '').strip()
        student_id = payload.get('student_id')

        if not material_id:
            return jsonify({'error': 'Material ID is required.'}), 400
        if not (student_input or student_id):
            return jsonify({'error': 'Student name or ID is required.'}), 400

        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found.'}), 404
            if not material[5]:
                return jsonify({'error': 'Material cannot be loaned without a QR code.'}), 400
            if (material[7] or 0) <= 0:
                return jsonify({'error': 'Material has zero copies and cannot be loaned.'}), 400
            if not material[3]:
                return jsonify({'error': 'Material is already loaned.'}), 400

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
                            'SELECT id, name FROM students WHERE lower(name)=lower(?) LIMIT 1',
                            (student_input,),
                        ).fetchone()
                        if row:
                            student_row = (row[0], row[1])
            if not student_row:
                return jsonify({'error': 'Student not found.'}), 404

            resolved_student_id = student_row[0]
            checkout_date = loan_material(int(material_id), int(resolved_student_id))
            if not checkout_date:
                return jsonify({'error': 'Material or student not found.'}), 404

            _invalidate_materials_cache()
            return jsonify(
                {
                    'status': 'loaned',
                    'material_id': material_id,
                    'student_id': resolved_student_id,
                    'student_name': student_row[1] if len(student_row) > 1 else None,
                    'checkout_date': checkout_date,
                }
            )
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/clear-loan', methods=['POST'])
    def api_bridge_materials_clear_loan():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('material_id')
        student_id = payload.get('student_id')

        if not material_id or not student_id:
            return jsonify({'error': 'Material ID and Student ID are required.'}), 400

        try:
            cleared_at = clear_active_material_loan(int(material_id), int(student_id))
            if not cleared_at:
                return jsonify({'error': 'No active loan found for this student and material.'}), 404
            _invalidate_materials_cache()
            return jsonify({'status': 'cleared', 'material_id': material_id, 'student_id': student_id, 'cleared_at': cleared_at})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/return', methods=['POST'])
    def api_bridge_materials_return():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('material_id')
        if not material_id:
            return jsonify({'error': 'Material ID is required.'}), 400
        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found.'}), 404
            if material[3]:
                return jsonify({'error': 'Material is already available.'}), 400
            return_date = return_material(int(material_id))
            _invalidate_materials_cache()
            return jsonify({'status': 'returned', 'material_id': material_id, 'return_date': return_date})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/save', methods=['POST'])
    def api_bridge_materials_save():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}

        title = (payload.get('title') or '').strip()
        author = (payload.get('author') or '').strip()
        publisher = (payload.get('publisher') or '').strip()
        qr_code = _normalize_qr_value(payload.get('qr_code')) if payload.get('qr_code') else None
        level = (payload.get('reading_level') or '').strip()
        copies = 1
        existing_id = payload.get('id')

        if qr_code:
            existing_by_qr = find_material_by_qr_code(qr_code)
            if existing_by_qr:
                if not existing_id or int(existing_id) != int(existing_by_qr[0]):
                    return jsonify({'error': 'QR code already exists and cannot be reused.'}), 409
            elif qr_token_exists(qr_code):
                return jsonify({'error': 'QR code was previously issued and cannot be reused.'}), 409

        if not existing_id and title:
            existing = find_material_by_title(title)
            if existing:
                existing_id = existing[0]

        if existing_id:
            try:
                update_material(
                    existing_id,
                    title=title or None,
                    author=author or None,
                    publisher=publisher or None,
                    qr_code=qr_code,
                    reading_level=level or None,
                    copies=copies,
                    available=1 if (copies > 0 and qr_code) else 0,
                    borrower_id=payload.get('borrower_id') if payload.get('borrower_id') is not None else None,
                )
                _invalidate_materials_cache(existing_id)
                return jsonify({'status': 'updated', 'id': existing_id})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        if not title:
            return jsonify({'error': 'Title is required.'}), 400

        try:
            new_id = add_material(
                title=title,
                author=author,
                publisher=publisher,
                qr_code=qr_code,
                available=1 if (copies > 0 and qr_code) else 0,
                reading_level=level,
                copies=copies,
            )
            _invalidate_materials_cache(new_id)
            return jsonify({'status': 'created', 'id': new_id})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/increase_copies', methods=['POST'])
    def api_bridge_materials_increase_copies():
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('id')
        additional_copies = _parse_non_negative_int(payload.get('additional_copies'), default=1)

        if not material_id:
            return jsonify({'error': 'Material ID is required.'}), 400

        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found.'}), 404

            current_copies = material[7] if len(material) > 7 else 1
            new_copies = current_copies + additional_copies
            update_material(material_id, copies=new_copies)
            _invalidate_materials_cache()
            return jsonify({'status': 'updated', 'id': material_id, 'new_copies': new_copies})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/<int:material_id>')
    def api_bridge_materials_get(material_id: int):
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found'}), 404
            data = _material_row_to_dict(material)
            if data.get('borrower_id'):
                student = get_student(data['borrower_id'])
                if student:
                    data['borrower_name'] = student[1]
            return jsonify({'material': data})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/bridge/materials/delete/<int:material_id>', methods=['POST'])
    def api_bridge_materials_delete(material_id: int):
        if not _bridge_request_is_pairing_authorized():
            return _bridge_pairing_auth_error()

        try:
            success = delete_material(material_id)
            if success:
                _invalidate_materials_cache()
                return jsonify({'success': True, 'deleted': True})
            return jsonify({'success': False, 'deleted': False, 'error': 'Material not found'}), 404
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/materials/add')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def materials_add():
        return render_template('material_add.html', edit_material_id=None)

    @app.route('/materials/edit/<int:material_id>')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def materials_edit(material_id: int):
        material = get_material(material_id)
        if not material:
            flash('Material not found', 'danger')
            return redirect(url_for('materials_list'))
        return render_template('material_add.html', edit_material_id=material_id)

    @app.route('/materials')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def materials_list():
        try:
            enforce_qr_availability_rule()
            rows = get_materials()
            payload = [_material_row_to_dict(row) for row in rows]
            return render_template('materials_list.html', materials=payload, total_materials=len(payload))
        except Exception as e:
            flash(f'Error loading materials: {str(e)}', 'danger')
            return render_template('materials_list.html', materials=[], total_materials=0)

    @app.route('/materials/loan')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def materials_loan_page():
        from datetime import datetime

        loaned_rows = get_loaned_materials_detailed()
        materials_by_student = {}
        for row in loaned_rows:
            student_name = row['student_name']
            if student_name not in materials_by_student:
                materials_by_student[student_name] = []

            checkout_date = row.get('checkout_date') or ''
            try:
                date_obj = datetime.fromisoformat(checkout_date.replace('Z', '+00:00'))
                formatted_date = date_obj.strftime('%Y-%m-%d')
            except Exception:
                formatted_date = checkout_date[:10] if len(checkout_date) >= 10 else checkout_date

            materials_by_student[student_name].append(
                {
                    'title': row.get('material_title') or '',
                    'checkout_date': formatted_date,
                    'student_id': row.get('student_id'),
                    'material_id': row.get('material_id'),
                    'loan_id': row.get('loan_id'),
                    'show_clear': bool(row.get('show_clear')),
                }
            )

        return render_template(
            'material_loan.html',
            materials_by_student=materials_by_student,
            total_loans=len(loaned_rows),
        )

    @app.route('/api/materials/catalog')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_catalog():
        forwarded = _bridge_forward_get('/api/bridge/materials/catalog')
        if forwarded is not None:
            return forwarded

        def _build_catalog_payload():
            enforce_qr_availability_rule()
            rows = get_materials()
            materials_payload = [_material_row_to_dict(row) for row in rows]
            return {'materials': materials_payload, 'count': len(materials_payload)}

        payload = server_cache.get_or_set(
            _materials_catalog_cache_key(),
            _build_catalog_payload,
            policy='book_catalog',
        )
        return jsonify(payload)

    @app.route('/api/materials/search')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_search():
        forwarded = _bridge_forward_get('/api/bridge/materials/search', request.args.to_dict(flat=True))
        if forwarded is not None:
            return forwarded

        query = request.args.get('q', '').strip().lower()
        level = request.args.get('level', '').strip()

        try:
            enforce_qr_availability_rule()
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                c = conn.cursor()

                sql = "SELECT id, title, author, available, reading_level, qr_code, publisher, copies, borrower_id FROM materials WHERE 1=1"
                params = []

                if query:
                    sql += " AND (title LIKE ? OR author LIKE ? OR publisher LIKE ? OR qr_code LIKE ?)"
                    search_term = f"%{query}%"
                    params.extend([search_term, search_term, search_term, search_term])

                if level:
                    sql += " AND reading_level = ?"
                    params.append(level)

                sql += " ORDER BY title"
                c.execute(sql, params)
                rows = c.fetchall()

            return jsonify({'materials': [_material_row_to_dict(row) for row in rows], 'count': len(rows)})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/levels')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_levels():
        forwarded = _bridge_forward_get('/api/bridge/materials/levels')
        if forwarded is not None:
            return forwarded

        try:
            with sqlite3.connect(DB_PATH, timeout=10) as conn:
                c = conn.cursor()
                c.execute('SELECT DISTINCT reading_level FROM materials WHERE reading_level IS NOT NULL ORDER BY reading_level')
                levels = [row[0] for row in c.fetchall()]
            return jsonify({'levels': levels})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/save', methods=['POST'])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_save():
        forwarded = _bridge_forward_post('/api/bridge/materials/save', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}

        title = (payload.get('title') or '').strip()
        author = (payload.get('author') or '').strip()
        publisher = (payload.get('publisher') or '').strip()
        qr_code = _normalize_qr_value(payload.get('qr_code')) if payload.get('qr_code') else None
        level = (payload.get('reading_level') or '').strip()
        copies = 1
        existing_id = payload.get('id')

        if qr_code:
            existing_by_qr = find_material_by_qr_code(qr_code)
            if existing_by_qr:
                if not existing_id or int(existing_id) != int(existing_by_qr[0]):
                    return jsonify({'error': 'QR code already exists and cannot be reused.'}), 409
            elif qr_token_exists(qr_code):
                return jsonify({'error': 'QR code was previously issued and cannot be reused.'}), 409

        if not existing_id and title:
            existing = find_material_by_title(title)
            if existing:
                existing_id = existing[0]

        if existing_id:
            try:
                update_material(
                    existing_id,
                    title=title or None,
                    author=author or None,
                    publisher=publisher or None,
                    qr_code=qr_code,
                    reading_level=level or None,
                    copies=copies,
                    available=1 if (copies > 0 and qr_code) else 0,
                    borrower_id=payload.get('borrower_id') if payload.get('borrower_id') is not None else None,
                )
                _invalidate_materials_cache(existing_id)
                return jsonify({'status': 'updated', 'id': existing_id})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        if not title:
            return jsonify({'error': 'Title is required.'}), 400

        try:
            new_id = add_material(
                title=title,
                author=author,
                publisher=publisher,
                qr_code=qr_code,
                available=1 if (copies > 0 and qr_code) else 0,
                reading_level=level,
                copies=copies,
            )
            _invalidate_materials_cache(new_id)
            return jsonify({'status': 'created', 'id': new_id})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/increase_copies', methods=['POST'])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_increase_copies():
        forwarded = _bridge_forward_post('/api/bridge/materials/increase_copies', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('id')
        additional_copies = _parse_non_negative_int(payload.get('additional_copies'), default=1)

        if not material_id:
            return jsonify({'error': 'Material ID is required.'}), 400

        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found.'}), 404

            current_copies = material[7] if len(material) > 7 else 1
            new_copies = current_copies + additional_copies
            update_material(material_id, copies=new_copies)
            _invalidate_materials_cache()
            return jsonify({'status': 'updated', 'id': material_id, 'new_copies': new_copies})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/loan', methods=['POST'])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_loan():
        forwarded = _bridge_forward_post('/api/bridge/materials/loan', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('material_id')
        student_input = (payload.get('student_input') or '').strip()
        student_id = payload.get('student_id')

        if not material_id:
            return jsonify({'error': 'Material ID is required.'}), 400
        if not (student_input or student_id):
            return jsonify({'error': 'Student name or ID is required.'}), 400

        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found.'}), 404
            if not material[5]:
                return jsonify({'error': 'Material cannot be loaned without a QR code.'}), 400
            if (material[7] or 0) <= 0:
                return jsonify({'error': 'Material has zero copies and cannot be loaned.'}), 400
            if not material[3]:
                return jsonify({'error': 'Material is already loaned.'}), 400

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
                            'SELECT id, name FROM students WHERE lower(name)=lower(?) LIMIT 1',
                            (student_input,),
                        ).fetchone()
                        if row:
                            student_row = (row[0], row[1])
            if not student_row:
                return jsonify({'error': 'Student not found.'}), 404

            resolved_student_id = student_row[0]
            checkout_date = loan_material(int(material_id), int(resolved_student_id))
            if not checkout_date:
                return jsonify({'error': 'Material or student not found.'}), 404

            try:
                pushed = sync_to_gdrive_now()
                if not pushed:
                    print('[sync] WARNING: immediate cloud push after device loan was skipped/failed.')
            except Exception as push_exc:
                print(f'[sync] WARNING: immediate cloud push after device loan error: {push_exc}')

            _invalidate_materials_cache()
            if _scanner_api_client_enabled() and bool(getattr(g, 'scanner_bridge_fallback', False)):
                scanner_sync.enqueue_mutation(
                    'material_loan',
                    {
                        'material_id': int(material_id),
                        'student_id': int(resolved_student_id),
                    },
                )
            return jsonify(
                {
                    'status': 'loaned',
                    'material_id': material_id,
                    'student_id': resolved_student_id,
                    'student_name': student_row[1] if len(student_row) > 1 else None,
                    'checkout_date': checkout_date,
                }
            )
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/clear-loan', methods=['POST'])
    @require_admin
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_clear_loan():
        forwarded = _bridge_forward_post('/api/bridge/materials/clear-loan', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('material_id')
        student_id = payload.get('student_id')

        if not material_id or not student_id:
            return jsonify({'error': 'Material ID and Student ID are required.'}), 400

        try:
            cleared_at = clear_active_material_loan(int(material_id), int(student_id))
            if not cleared_at:
                return jsonify({'error': 'No active loan found for this student and material.'}), 404
            _invalidate_materials_cache()
            if _scanner_api_client_enabled() and bool(getattr(g, 'scanner_bridge_fallback', False)):
                scanner_sync.enqueue_mutation(
                    'material_clear_loan',
                    {
                        'material_id': int(material_id),
                        'student_id': int(student_id),
                    },
                )
            return jsonify({'status': 'cleared', 'material_id': material_id, 'student_id': student_id, 'cleared_at': cleared_at})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/return', methods=['POST'])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_return():
        forwarded = _bridge_forward_post('/api/bridge/materials/return', request.get_json(silent=True) or {})
        if forwarded is not None:
            return forwarded

        payload = request.get_json(silent=True) or {}
        material_id = payload.get('material_id')
        if not material_id:
            return jsonify({'error': 'Material ID is required.'}), 400
        try:
            material = get_material(material_id)
            if not material:
                return jsonify({'error': 'Material not found.'}), 404
            if material[3]:
                return jsonify({'error': 'Material is already available.'}), 400
            return_date = return_material(int(material_id))
            _invalidate_materials_cache()
            if _scanner_api_client_enabled() and bool(getattr(g, 'scanner_bridge_fallback', False)):
                scanner_sync.enqueue_mutation(
                    'material_return',
                    {
                        'material_id': int(material_id),
                    },
                )
            return jsonify({'status': 'returned', 'material_id': material_id, 'return_date': return_date})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/materials/sync-student-status', methods=['POST'])
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_sync_student_status():
        backup_path = db_backup_recovery.create_backup('materials_sync_student_status')
        cache_invalidators = (
            lambda: _invalidate_material_sync_caches(),
        )
        try:
            count = sync_all_students_material_status()
            invalidate_scoped_cache(*cache_invalidators)
            return jsonify({'status': 'success', 'students_updated': count, 'backup': backup_path})
        except Exception as e:
            return json_scoped_failure(
                backup_path=backup_path,
                table_names=('students', 'materials'),
                error=e,
                invalidators=cache_invalidators,
            )

    @app.route('/api/materials/<int:material_id>')
    @require_login
    @require_feature(auth_manager.FEATURE_BOOKS)
    def api_materials_get(material_id: int):
        forwarded = _bridge_forward_get(f'/api/bridge/materials/{material_id}')
        if forwarded is not None:
            return forwarded

        try:
            cache_key = _material_detail_cache_key(material_id)

            def _build_material_detail_payload():
                material = get_material(material_id)
                if not material:
                    return None
                data = _material_row_to_dict(material)
                if data.get('borrower_id'):
                    student = get_student(data['borrower_id'])
                    if student:
                        data['borrower_name'] = student[1]
                return {'material': data}

            payload = server_cache.get_or_set(
                cache_key,
                _build_material_detail_payload,
                policy='book_catalog',
            )
            if payload is None:
                return jsonify({'error': 'Material not found'}), 404
            return jsonify(payload)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/materials/delete/<int:material_id>', methods=['POST'])
    @require_admin
    @require_feature(auth_manager.FEATURE_BOOKS)
    def materials_delete(material_id: int):
        if _scanner_api_client_enabled():
            forwarded = _bridge_forward_post(f'/api/bridge/materials/delete/{material_id}', {})
            if forwarded is not None:
                response, status_code = forwarded
                payload = response.get_json(silent=True) or {}
                if 200 <= int(status_code) < 300 and payload.get('deleted'):
                    flash('Material deleted', 'success')
                else:
                    flash(payload.get('error') or 'Delete failed on Instructor Station.', 'danger')
                return redirect(url_for('materials_list'))

        try:
            success = delete_material(material_id)
            if success:
                _invalidate_materials_cache()
                flash('Material deleted', 'success')
            else:
                flash('Material not found', 'warning')
        except Exception as e:
            flash(f'Delete failed: {e}', 'danger')
        return redirect(url_for('materials_list'))
