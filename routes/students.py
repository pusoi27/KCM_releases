# routes/students.py
from io import BytesIO, StringIO
import csv

from flask import abort, jsonify, render_template, request, redirect, url_for, flash, send_file, current_app, after_this_request
from werkzeug.utils import secure_filename
from modules import student_manager, instructor_profile_manager, server_cache, db_backup_recovery, auth_manager
from routes.auth import require_login, require_admin, require_feature
from routes.operation_utils import flash_scoped_failure, invalidate_scoped_cache
import os
import tempfile
import sqlite3
from modules.database import DB_PATH, get_station_runtime_config
import json
import re
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
_ALLOWED_PHOTO_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_SUBJECTS = 3
MAX_SCHEDULE_DAYS = 6
MAX_STUDENT_IDENTIFIER_LEN = 64
_STUDENT_IDENTIFIER_RE = re.compile(r'^[A-Za-z0-9]*$')


def _parse_student_identifier(raw_value: str) -> str:
    """Validate and normalize Student ID input."""
    value = student_manager.normalize_student_identifier(raw_value, MAX_STUDENT_IDENTIFIER_LEN)
    if len(value) > MAX_STUDENT_IDENTIFIER_LEN:
        raise ValueError(f"Student ID must be {MAX_STUDENT_IDENTIFIER_LEN} characters or fewer.")
    if value and not _STUDENT_IDENTIFIER_RE.fullmatch(value):
        raise ValueError("Student ID can contain letters and numbers only.")
    return value


def _build_student_badges_pdf(students):
    """Build A4 landscape student badge PDF using 3.5" x 2" business cards.

    Layout requirements:
    - A4 landscape page
    - Card size: 3.5" x 2" (landscape)
    - Fit as many cards as possible per sheet
    - Name on top row
    - Photo left and QR right, equal proportions
    """
    buffer = BytesIO()
    page_w, page_h = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=(page_w, page_h))

    card_w = 88.9 * mm   # 3.5 inches
    card_h = 50.8 * mm   # 2.0 inches
    cols = max(1, int(page_w // card_w))
    rows = max(1, int(page_h // card_h))

    used_w = cols * card_w
    used_h = rows * card_h
    margin_x = max(0, (page_w - used_w) / 2)
    margin_y = max(0, (page_h - used_h) / 2)
    gap_x = 0
    gap_y = 0

    cards_per_page = cols * rows

    for index, student in enumerate(students):
        slot = index % cards_per_page
        if index and slot == 0:
            pdf.showPage()

        row = slot // cols
        col = slot % cols
        x = margin_x + col * (card_w + gap_x)
        y = page_h - margin_y - (row + 1) * card_h - row * gap_y

        student_name = str(student[1] or '').strip()
        student_identifier = str(student[2] or '').strip()
        photo_blob = student[3]
        qr_blob = student[5]

        if isinstance(photo_blob, memoryview):
            photo_blob = photo_blob.tobytes()
        if isinstance(qr_blob, memoryview):
            qr_blob = qr_blob.tobytes()

        padding = 2.5 * mm
        inner_w = card_w - (2 * padding)
        inner_h = card_h - (2 * padding)
        name_h = 9 * mm
        id_h = 6 * mm if student_identifier else 0
        media_h = max(10 * mm, inner_h - name_h - id_h)
        image_gap = 2 * mm
        media_w_each = max(10 * mm, (inner_w - image_gap) / 2)
        image_side = max(10 * mm, min(media_w_each, media_h))

        top_y = y + card_h - padding
        name_y = top_y - 6.5 * mm

        pdf.setStrokeColorRGB(0.75, 0.75, 0.75)
        pdf.setLineWidth(0.4)
        pdf.rect(x, y, card_w, card_h, stroke=1, fill=0)

        pdf.setFont("Helvetica-Bold", 10)
        display_name = student_name if len(student_name) <= 34 else student_name[:31] + "..."
        pdf.drawCentredString(x + card_w / 2, name_y, display_name)

        media_y = y + padding + (id_h if id_h else 0) + max(0, (media_h - image_side) / 2)
        if student_identifier:
            pdf.setFont("Helvetica", 7)
            pdf.drawCentredString(x + card_w / 2, y + padding + 1 * mm, f"ID: {student_identifier}")

        photo_x = x + padding
        qr_x = photo_x + image_side + image_gap

        if photo_blob:
            try:
                pdf.drawImage(
                    ImageReader(BytesIO(photo_blob)),
                    photo_x,
                    media_y,
                    width=image_side,
                    height=image_side,
                    preserveAspectRatio=True,
                    mask='auto',
                )
            except Exception:
                photo_blob = None
        if not photo_blob:
            pdf.setStrokeColorRGB(0.82, 0.82, 0.82)
            pdf.rect(photo_x, media_y, image_side, image_side, stroke=1, fill=0)
            pdf.setFont("Helvetica", 5.5)
            pdf.drawCentredString(photo_x + image_side / 2, media_y + image_side / 2, "No Photo")

        if qr_blob:
            try:
                pdf.drawImage(
                    ImageReader(BytesIO(qr_blob)),
                    qr_x,
                    media_y,
                    width=image_side,
                    height=image_side,
                    preserveAspectRatio=True,
                    mask='auto',
                )
            except Exception:
                qr_blob = None
        if not qr_blob:
            pdf.setStrokeColorRGB(0.82, 0.82, 0.82)
            pdf.rect(qr_x, media_y, image_side, image_side, stroke=1, fill=0)
            pdf.setFont("Helvetica", 5.5)
            pdf.drawCentredString(qr_x + image_side / 2, media_y + image_side / 2, "No QR")

    pdf.save()
    buffer.seek(0)
    return buffer


def _vcf_escape(value: str) -> str:
    """Escape VCF text values per RFC-style expectations."""
    text = str(value or "")
    return (
        text.replace('\\', '\\\\')
        .replace('\n', '\\n')
        .replace(';', '\\;')
        .replace(',', '\\,')
    )


def _build_student_vcf(student_row) -> str:
    """Build a single-contact VCF payload for a student."""
    student_name = str(student_row[1] or '').strip() if len(student_row) > 1 else 'Student'
    email = str(student_row[3] or '').strip() if len(student_row) > 3 else ''
    phone = str(student_row[4] or '').strip() if len(student_row) > 4 else ''
    guardian = str(student_row[22] or '').strip() if len(student_row) > 22 else ''

    first_name_field = f"{student_name} ({guardian})" if guardian else student_name
    fn_value = first_name_field

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:;{_vcf_escape(first_name_field)};;;",
        f"FN:{_vcf_escape(fn_value)}",
    ]
    if phone:
        lines.append(f"TEL;TYPE=CELL:{_vcf_escape(phone)}")
    if email:
        lines.append(f"EMAIL;TYPE=INTERNET:{_vcf_escape(email)}")
    lines.append("END:VCARD")

    return "\r\n".join(lines) + "\r\n"


def _vcf_unescape(value: str) -> str:
    """Decode common VCF escape sequences."""
    text = str(value or "")
    return (
        text.replace('\\n', '\n')
        .replace('\\N', '\n')
        .replace('\\,', ',')
        .replace('\\;', ';')
        .replace('\\\\', '\\')
        .strip()
    )


def _unfold_vcf_lines(vcf_text: str) -> list[str]:
    """Unfold folded VCF lines (continuation lines start with space/tab)."""
    raw_lines = str(vcf_text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    unfolded = []
    for line in raw_lines:
        if not line:
            unfolded.append('')
            continue
        if line.startswith(' ') or line.startswith('\t'):
            if unfolded:
                unfolded[-1] += line[1:]
            continue
        unfolded.append(line)
    return unfolded


def _extract_vcard_blocks(vcf_text: str) -> list[list[str]]:
    """Return list of VCF blocks, each block as a list of unfolded lines."""
    lines = _unfold_vcf_lines(vcf_text)
    blocks = []
    current = []
    in_card = False

    for line in lines:
        upper = line.strip().upper()
        if upper == 'BEGIN:VCARD':
            in_card = True
            current = [line]
            continue
        if in_card:
            current.append(line)
            if upper == 'END:VCARD':
                blocks.append(current)
                current = []
                in_card = False

    return blocks


def _read_vcard_values(card_lines: list[str], field_name: str) -> list[str]:
    """Extract all values for a VCF field name (supports params like TEL;TYPE=...)."""
    prefix = f"{field_name.upper()}"
    values = []
    for line in card_lines:
        if ':' not in line:
            continue
        left, right = line.split(':', 1)
        left_upper = left.upper()
        if left_upper == prefix or left_upper.startswith(prefix + ';'):
            values.append(_vcf_unescape(right))
    return values


def _normalize_contact_name(raw_name: str) -> str:
    """Map contact full name to student display form: FirstName LastInitial."""
    if not raw_name:
        return ''
    base_name = re.sub(r'\(.*?\)', '', str(raw_name)).strip()
    parts = [p for p in base_name.split() if p]
    if not parts:
        return ''
    first_name = parts[0]
    if len(parts) >= 2:
        return f"{first_name} {parts[-1][0]}."
    return first_name


def _extract_guardian_name(raw_name: str) -> str:
    """Extract guardian from first parenthesized segment in contact name."""
    if not raw_name:
        return ''
    match = re.search(r'\((.*?)\)', str(raw_name))
    if not match:
        return ''
    return str(match.group(1) or '').strip()


def _split_vcard_student_names(raw_name: str) -> list[str]:
    """Expand a VCF contact name into one or more student display names.

    Examples:
    - "Ja'Sean Osceola" -> ["Ja'Sean O."]
    - "Ja'Sean & Journi & Jail'a Osceola" ->
      ["Ja'Sean O.", "Journi O.", "Jail'a O."]
    """
    if not raw_name:
        return []

    base_name = re.sub(r'\(.*?\)', '', str(raw_name)).strip()
    if not base_name:
        return []

    name_chunks = [chunk.strip() for chunk in re.split(r'\s*&\s*', base_name) if chunk.strip()]
    if len(name_chunks) <= 1:
        normalized = _normalize_contact_name(base_name)
        return [normalized] if normalized else []

    surname = ''
    for chunk in reversed(name_chunks):
        parts = [part for part in chunk.split() if part]
        if len(parts) >= 2:
            surname = parts[-1]
            break

    if not surname:
        normalized = _normalize_contact_name(base_name)
        return [normalized] if normalized else []

    names = []
    seen = set()
    for chunk in name_chunks:
        parts = [part for part in chunk.split() if part]
        if not parts:
            continue
        student_name = f"{parts[0]} {surname[0]}."
        if student_name in seen:
            continue
        seen.add(student_name)
        names.append(student_name)

    return names


def _normalize_vcard_subject_value(raw_value: str) -> str:
    """Normalize a VCF note subject hint into a canonical subject name."""
    token = str(raw_value or '').strip().lower()
    if not token:
        return ''

    if token.startswith(('math', 'mat', 'm')):
        return 'Math'
    if token.startswith(('reading', 'read', 'rea', 'r')):
        return 'Reading'
    if token.startswith(('writing', 'write', 'wri', 'w')):
        return 'Writing'
    return ''


def _subjects_from_vcard_notes(card_lines: list[str]) -> list[str]:
    """Extract Math/Reading/Writing subject names from VCF NOTE fields."""
    subjects = []
    seen = set()
    for note in _read_vcard_values(card_lines, 'NOTE'):
        for match in re.finditer(r'(?i)\bsubject\s*:\s*([^\n,;]+)', note):
            subject = _normalize_vcard_subject_value(match.group(1))
            if not subject or subject in seen:
                continue
            seen.add(subject)
            subjects.append(subject)
    return subjects


def _full_name_from_vcard(card_lines: list[str]) -> str:
    """Get best available full name from FN, then N fields."""
    fn_values = _read_vcard_values(card_lines, 'FN')
    if fn_values and fn_values[0].strip():
        return fn_values[0].strip()

    n_values = _read_vcard_values(card_lines, 'N')
    if n_values:
        # N format: Last;First;Middle;Prefix;Suffix
        parts = (n_values[0] or '').split(';')
        last = parts[0].strip() if len(parts) > 0 else ''
        first = parts[1].strip() if len(parts) > 1 else ''
        candidate = ' '.join([p for p in [first, last] if p]).strip()
        if candidate:
            return candidate
    return ''


def _student_identifier_from_vcard(card_lines: list[str]) -> str:
    """Extract optional student identifier from known VCF fields."""
    for field_name in ('X-STUDENT-ID', 'X-STDYTIME-STUDENT-ID', 'STUDENTID', 'UID'):
        values = _read_vcard_values(card_lines, field_name)
        if not values:
            continue
        normalized = student_manager.normalize_student_identifier(values[0])
        if normalized:
            return normalized
    return ''


def _parse_vcf_contacts(vcf_text: str) -> list[dict]:
    """Parse VCF text into normalized student-import contact dictionaries."""
    contacts = []
    for block in _extract_vcard_blocks(vcf_text):
        full_name = _full_name_from_vcard(block)
        email_values = _read_vcard_values(block, 'EMAIL')
        tel_values = _read_vcard_values(block, 'TEL')

        email = str(email_values[0] or '').strip() if email_values else ''
        phone = str(tel_values[0] or '').strip() if tel_values else ''
        student_names = _split_vcard_student_names(full_name)
        guardian = _extract_guardian_name(full_name)
        student_identifier = _student_identifier_from_vcard(block)
        subjects = _subjects_from_vcard_notes(block)

        if not student_names:
            continue

        match_on_email = len(student_names) == 1
        match_on_identifier = len(student_names) == 1
        for student_name in student_names:
            contacts.append(
                {
                    'student_name': student_name,
                    'guardian': guardian,
                    'email': email,
                    'phone': phone,
                    'student_identifier': student_identifier,
                    'subjects': subjects,
                    'match_on_email': match_on_email,
                    'match_on_identifier': match_on_identifier,
                }
            )
    return contacts


def _read_student_photo(file_storage, student_id):
    """Validate and read an uploaded photo as raw bytes plus mime type."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    if ext not in _ALLOWED_PHOTO_EXTS:
        return None
    photo_bytes = file_storage.read()
    if not photo_bytes:
        return None
    max_photo_bytes = int(current_app.config.get('MAX_PHOTO_BYTES') or 0)
    if max_photo_bytes and len(photo_bytes) > max_photo_bytes:
        return None
    photo_mime = file_storage.mimetype or {
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'webp': 'image/webp',
    }.get(ext, 'image/png')
    return photo_bytes, photo_mime


def _parse_subjects_from_form(form):
    """Parse dynamic subject rows from form payload.

    Supports both new array fields and legacy single-subject fields.
    """
    subjects = []
    minutes = []

    raw_subjects = form.getlist("subject_name[]")
    raw_minutes = form.getlist("subject_minutes[]")

    if raw_subjects:
        for idx, raw in enumerate(raw_subjects):
            subject = (raw or "").strip()
            if not subject:
                continue
            minute_val = 30
            if idx < len(raw_minutes):
                try:
                    minute_val = max(5, int(str(raw_minutes[idx]).strip() or "30"))
                except ValueError:
                    minute_val = 30
            subjects.append(subject)
            minutes.append(minute_val)

    # Backward compatibility with legacy single-subject form.
    if not subjects:
        subject = (form.get("subject", "").strip() or "")
        if subject == "New":
            subject = form.get("custom_subject", "").strip()
        if subject:
            subjects = [subject]
            minutes = [30]

    return subjects[:MAX_SUBJECTS], minutes[:MAX_SUBJECTS]


def _normalize_schedule_json(schedule_json_str):
    """Normalize schedule payload and keep all unique selected days."""
    entries = []
    if schedule_json_str:
        try:
            entries = json.loads(schedule_json_str)
        except (TypeError, ValueError):
            entries = []

    cleaned = []
    seen_days = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        day = str(entry.get("day") or "").strip()
        time = str(entry.get("time") or "").strip()
        if not day or day in seen_days:
            continue
        seen_days.add(day)
        cleaned.append({"day": day, "time": time})
        if len(cleaned) >= MAX_SCHEDULE_DAYS:
            break

    return json.dumps(cleaned)


def _extract_days(schedule_json_str):
    """Preserve up to six day/time fields from the schedule entries."""
    entries = []
    if schedule_json_str:
        try:
            entries = json.loads(schedule_json_str)
        except (TypeError, ValueError):
            entries = []

    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        day = str(entry.get("day") or "").strip()
        time = str(entry.get("time") or "").strip()
        if day:
            normalized.append((day, time))

    normalized = normalized[:MAX_SCHEDULE_DAYS]
    result = {}
    for idx in range(1, MAX_SCHEDULE_DAYS + 1):
        if idx <= len(normalized):
            result[f"day{idx}"] = normalized[idx - 1][0]
            result[f"day{idx}_time"] = normalized[idx - 1][1]
        else:
            result[f"day{idx}"] = ""
            result[f"day{idx}_time"] = ""
    return result


def _students_list_cache_key() -> str:
    return server_cache.STUDENTS_LIST_CACHE_KEY

def _invalidate_student_caches():
    """Invalidate student cache lanes."""
    server_cache.invalidate(_students_list_cache_key())


def _student_photo_url(student_row):
    if not student_row:
        return ''
    raw = student_row[19] if len(student_row) > 19 else None
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    has_blob = isinstance(raw, (bytes, bytearray)) and len(raw) > 0
    return url_for('students_photo', sid=student_row[0]) if has_blob else ''


def _guardian_contacts_from_student_row(row):
    """Return deduplicated guardian contacts from primary + secondary fields."""
    contacts = []
    seen = set()
    candidates = [
        (str(row[3] or '').strip() if len(row) > 3 else '', str(row[22] or '').strip() if len(row) > 22 else ''),
        (str(row[34] or '').strip() if len(row) > 34 else '', str(row[36] or '').strip() if len(row) > 36 else ''),
    ]

    for email, guardian in candidates:
        if not email or '@' not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        contacts.append({'email': email, 'guardian': guardian})

    return contacts


def register_student_routes(app, upload_folder):
    """Register student CRUD and CSV routes."""
    
    @app.route("/students")
    @require_login
    def students_list():
        runtime = get_station_runtime_config()
        if str(runtime.get('station_mode') or '').strip().lower() == 'scanner_api_client':
            return render_template(
                "students_live_readonly.html",
                instructor_api_base_url=str(runtime.get('instructor_api_base_url') or '').strip().rstrip('/'),
            )

        duplicate_count = student_manager.get_duplicate_name_count()
        
        return render_template(
            "students.html",
            students=student_manager.get_student_database_rows(),
            deleted_students=student_manager.get_student_database_rows(active=0),
            has_duplicates=duplicate_count > 0,
            duplicate_count=duplicate_count,
        )

    @app.route("/students/notify", methods=["POST"])
    @require_login
    def students_notify():
        from modules.email_manager import get_email_manager, render_branded_email_shell, resolve_center_name
        from modules import instructor_profile_manager as _ipm

        raw_ids = request.form.getlist("student_ids")
        subject = request.form.get("subject", "").strip()
        message = request.form.get("message", "").strip()

        if not raw_ids:
            flash("No students selected.", "warning")
            return redirect(url_for("students_list"))
        if not subject or not message:
            flash("Subject and message are required.", "danger")
            return redirect(url_for("students_list"))

        # Validate and deduplicate IDs
        try:
            student_ids = list({int(sid) for sid in raw_ids if str(sid).strip().isdigit()})
        except (ValueError, TypeError):
            flash("Invalid student selection.", "danger")
            return redirect(url_for("students_list"))

        if not student_ids:
            flash("No valid students selected.", "warning")
            return redirect(url_for("students_list"))

        profile = _ipm.get_instructor_profile() or {}
        center_name = resolve_center_name()
        email_manager = get_email_manager()

        sent = []
        skipped = []
        failed = []

        for sid in student_ids:
            row = student_manager.get_student(sid)
            if not row:
                skipped.append(f"ID {sid} (not found)")
                continue
            student_name = str(row[1] or "Student").strip()
            contacts = _guardian_contacts_from_student_row(row)
            if not contacts:
                skipped.append(student_name)
                continue

            sent_any = False
            failed_details = []
            for contact in contacts:
                recipient_email = contact['email']
                guardian = contact['guardian']
                salutation = f"Dear {guardian}," if guardian else "Dear Parent/Guardian,"
                plain_body = (
                    f"{salutation}\n\n"
                    f"{message}\n\n"
                    f"— {center_name}"
                )
                html_body = render_branded_email_shell(
                    title=subject,
                    center_name=center_name,
                    body_html=(
                        f"<p>{salutation}</p>"
                        f"<p>{'<br>'.join(line if line.strip() else '&nbsp;' for line in message.splitlines())}</p>"
                    ),
                    footer_note=f"This message was sent from {center_name}. Please do not reply to this email.",
                )

                result = email_manager.send_email(
                    recipient_email=recipient_email,
                    subject=f"{center_name} — {subject}",
                    body=plain_body,
                    html_body=html_body,
                )
                if result.get("success"):
                    sent_any = True
                else:
                    failed_details.append(f"{recipient_email}: {result.get('error', 'unknown error')}")

            if sent_any:
                sent.append(student_name)
            if failed_details:
                failed.append(f"{student_name} ({'; '.join(failed_details)})")

        parts = []
        if sent:
            parts.append(f"Sent to {len(sent)} student(s): {', '.join(sent)}.")
        if skipped:
            parts.append(f"Skipped (no email): {', '.join(skipped)}.")
        if failed:
            parts.append(f"Failed: {', '.join(failed)}.")

        if sent and not failed:
            flash(" ".join(parts), "success")
        elif failed:
            flash(" ".join(parts), "warning")
        else:
            flash(" ".join(parts) if parts else "No emails sent.", "warning")

        return redirect(url_for("students_list"))

    @app.route("/students/checkout-notify", methods=["POST"])
    @require_login
    def students_checkout_notify_update():
        payload = request.get_json(silent=True) or {}
        sid = payload.get("student_id")
        enabled = payload.get("enabled")

        try:
            sid = int(sid)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Invalid student_id"}), 400

        student = student_manager.get_student(sid)
        if not student:
            return jsonify({"success": False, "error": "Student not found"}), 404

        has_email = bool(str(student[3] or "").strip()) if len(student) > 3 else False
        if not has_email and bool(enabled):
            return jsonify({"success": False, "error": "Student has no email on file"}), 400

        student_manager.set_checkout_notify_enabled(sid, bool(enabled))
        _invalidate_student_caches()
        return jsonify({
            "success": True,
            "student_id": sid,
            "checkout_notify_enabled": bool(enabled),
        })

    @app.route("/students/photo/<int:sid>")
    @require_login
    def students_photo(sid):
        photo = student_manager.get_student_photo(sid)
        if not photo or not photo.get('photo_blob'):
            abort(404)
        response = send_file(
            BytesIO(photo['photo_blob']),
            mimetype=photo.get('photo_mime') or 'image/png',
            download_name=f'student_{sid}.png',
            max_age=0,
        )
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        return response

    @app.route("/students/duplicates")
    @require_login
    def students_duplicates():
        """Display all duplicate student names with their details."""
        duplicate_summary = student_manager.get_duplicate_summary()
        return render_template(
            "students_duplicates.html",
            duplicate_summary=duplicate_summary,
            has_duplicates=len(duplicate_summary) > 0,
        )

    @app.route("/api/students/duplicates")
    @require_login
    def api_get_duplicates():
        """API endpoint to get duplicate student names (JSON response)."""
        from flask import jsonify
        duplicate_summary = student_manager.get_duplicate_summary()
        has_duplicates = len(duplicate_summary) > 0
        
        return jsonify({
            'success': True,
            'has_duplicates': has_duplicates,
            'total_duplicate_names': len(duplicate_summary),
            'duplicates': duplicate_summary
        })


    @app.route("/students/add", methods=["GET", "POST"])
    @require_login
    def students_add():
        if request.method == "POST":
            try:
                student_identifier = _parse_student_identifier(request.form.get("student_identifier", ""))
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("students_add"))

            subjects, subject_minutes = _parse_subjects_from_form(request.form)
            if not subjects:
                flash("Please add at least one subject.", "danger")
                return redirect(url_for("students_add"))

            _sched_json = _normalize_schedule_json(request.form.get("schedule_json", ""))
            schedule_fields = _extract_days(_sched_json)
            student_id = student_manager.add_student(
                request.form["name"],
                subjects[0],
                request.form.get("email", ""),
                request.form.get("phone", ""),
                book_loaned=int(bool(request.form.get("book_loaned"))),
                el=int(bool(request.form.get("el"))),
                pi=int(bool(request.form.get("pi"))),
                v=int(bool(request.form.get("v"))),
                ind=int(bool(request.form.get("ind"))),
                **schedule_fields,
                subjects=subjects,
                subject_minutes=subject_minutes,
                schedule_json=_sched_json,
                guardian=request.form.get("guardian", ""),
                student_identifier=student_identifier,
                secondary_email=request.form.get("email_secondary", ""),
                secondary_phone=request.form.get("phone_secondary", ""),
                secondary_guardian=request.form.get("guardian_secondary", ""),
            )
            # Invalidate tenant-scoped student list lane.
            _invalidate_student_caches()
            # Save photo after we have the student_id
            photo_file = request.files.get('photo')
            if photo_file and photo_file.filename:
                photo_info = _read_student_photo(photo_file, student_id)
                if photo_info:
                    photo_bytes, photo_mime = photo_info
                    student_manager.set_student_photo(student_id, photo_bytes, photo_mime)
            flash("Student added successfully.", "success")
            return redirect(url_for("students_list"))
        
        # Get instructor profile for class hours
        profile = instructor_profile_manager.get_instructor_profile()
        subject_rows = [
            {"name": "Math", "minutes": 30, "selected": True},
            {"name": "Reading", "minutes": 30, "selected": False},
            {"name": "Writing", "minutes": 30, "selected": False},
        ]
        return render_template("student_form.html", action="Add", student=None, profile=profile, subject_rows=subject_rows, student_photo_url='', student_schedule=[])

    @app.route("/students/edit/<int:sid>", methods=["GET", "POST"])
    @require_login
    def students_edit(sid):
        stu = student_manager.get_student(sid)
        if not stu:
            return "Student not found", 404
        if request.method == "POST":
            try:
                student_identifier = _parse_student_identifier(request.form.get("student_identifier", ""))
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("students_edit", sid=sid))

            subjects, subject_minutes = _parse_subjects_from_form(request.form)
            if not subjects:
                flash("Please add at least one subject.", "danger")
                return redirect(url_for("students_edit", sid=sid))

            _sched_json = _normalize_schedule_json(request.form.get("schedule_json", ""))
            schedule_fields = _extract_days(_sched_json)
            student_manager.update_student(
                sid,
                request.form["name"],
                request.form.get("email", ""),
                request.form.get("phone", ""),
                subject=subjects[0],
                book_loaned=int(bool(request.form.get("book_loaned"))),
                el=int(bool(request.form.get("el"))),
                pi=int(bool(request.form.get("pi"))),
                v=int(bool(request.form.get("v"))),
                ind=int(bool(request.form.get("ind"))),
                **schedule_fields,
                subjects=subjects,
                subject_minutes=subject_minutes,
                schedule_json=_sched_json,
                guardian=request.form.get("guardian", ""),
                student_identifier=student_identifier,
                secondary_email=request.form.get("email_secondary", ""),
                secondary_phone=request.form.get("phone_secondary", ""),
                secondary_guardian=request.form.get("guardian_secondary", ""),
            )
            # Invalidate tenant-scoped student list lane.
            _invalidate_student_caches()
            # Save photo if a new one was uploaded
            photo_file = request.files.get('photo')
            if photo_file and photo_file.filename:
                photo_info = _read_student_photo(photo_file, sid)
                if photo_info:
                    photo_bytes, photo_mime = photo_info
                    student_manager.set_student_photo(sid, photo_bytes, photo_mime)
            flash("Student updated.", "info")
            # Check if came from calendar
            from_calendar = request.args.get('from_calendar')
            if from_calendar:
                return redirect(url_for('center_calendar'))
            return redirect(url_for("students_list"))
        
        # Get instructor profile for class hours
        profile = instructor_profile_manager.get_instructor_profile()
        from_calendar = request.args.get('from_calendar')
        subjects = []
        minutes = []
        if len(stu) > 16 and stu[16]:
            try:
                subjects = json.loads(stu[16])
            except (TypeError, ValueError):
                subjects = []
        if len(stu) > 17 and stu[17]:
            try:
                minutes = json.loads(stu[17])
            except (TypeError, ValueError):
                minutes = []
        if not subjects:
            subjects = [stu[2]] if len(stu) > 2 and stu[2] else [""]
        if not minutes:
            minutes = [30] * len(subjects)

        subject_rows = []
        for idx, subject_name in enumerate(subjects):
            minute_val = 30
            if idx < len(minutes):
                try:
                    minute_val = max(5, int(minutes[idx]))
                except (TypeError, ValueError):
                    minute_val = 30
            subject_rows.append({"name": str(subject_name or ""), "minutes": minute_val})
        if not subject_rows:
            subject_rows = [
                {"name": "Math", "minutes": 30, "selected": True},
                {"name": "Reading", "minutes": 30, "selected": False},
                {"name": "Writing", "minutes": 30, "selected": False},
            ]
        subject_rows = subject_rows[:MAX_SUBJECTS]

        student_schedule = []
        schedule_json = stu[21] if len(stu) > 21 else ''
        if schedule_json:
            try:
                student_schedule = json.loads(schedule_json)
            except (ValueError, TypeError):
                pass
        if not student_schedule:
            if stu[12]:
                student_schedule.append({'day': stu[12], 'time': stu[14] or ''})
            if stu[13]:
                student_schedule.append({'day': stu[13], 'time': stu[15] or ''})
            if len(stu) > 25 and stu[25]:
                student_schedule.append({'day': stu[25], 'time': stu[26] or ''})
            if len(stu) > 27 and stu[27]:
                student_schedule.append({'day': stu[27], 'time': stu[28] or ''})
            if len(stu) > 29 and stu[29]:
                student_schedule.append({'day': stu[29], 'time': stu[30] or ''})
            if len(stu) > 31 and stu[31]:
                student_schedule.append({'day': stu[31], 'time': stu[32] or ''})
        student_schedule = student_schedule[:MAX_SCHEDULE_DAYS]
        return render_template(
            "student_form.html",
            action="Edit",
            student=stu,
            profile=profile,
            from_calendar=from_calendar,
            subject_rows=subject_rows,
            student_photo_url=_student_photo_url(stu),
            student_schedule=student_schedule,
        )

    @app.route("/students/delete/<int:sid>", methods=["POST"])
    @require_admin
    def students_delete(sid):
        student_manager.delete_student(sid)
        _invalidate_student_caches()
        flash("Student deleted.", "warning")
        return redirect(url_for("students_list"))

    @app.route("/students/reactivate/<int:sid>", methods=["POST"])
    @require_admin
    def students_reactivate(sid):
        student_manager.reactivate_student(sid)
        _invalidate_student_caches()
        flash("Student reactivated.", "success")
        return redirect(url_for("students_list"))

    @app.route("/students/permanent-delete/<int:sid>", methods=["POST"])
    @require_admin
    def students_permanent_delete(sid):
        student_manager.permanent_delete_student(sid)
        _invalidate_student_caches()
        flash("Student permanently deleted.", "danger")
        return redirect(url_for("students_list"))

    @app.route("/students/import", methods=["POST"])
    @require_login
    def students_import():
        file = request.files.get("csvfile")
        if not file or file.filename == "":
            flash("No file selected.", "danger")
            return redirect(url_for("students_list"))
        path = os.path.join(upload_folder, secure_filename(file.filename))
        file.save(path)
        backup_path = db_backup_recovery.create_backup("students_import")
        cache_invalidators = [
            lambda: _invalidate_student_caches()
        ]
        try:
            result = student_manager.import_csv(path)
            if isinstance(result, dict) and result.get("error"):
                flash_scoped_failure(
                    backup_path=backup_path,
                    table_names=("students",),
                    error=result.get("error"),
                    invalidators=cache_invalidators,
                    category="danger",
                )
                flash("Import failed. Please check the CSV file and try again.", "danger")
                return redirect(url_for("students_list"))
        except Exception as e:
            flash_scoped_failure(
                backup_path=backup_path,
                table_names=("students",),
                error=e,
                invalidators=cache_invalidators,
                category="danger",
            )
            flash("Import failed. Please check the CSV file and try again.", "danger")
            return redirect(url_for("students_list"))
        added = result.get("added", 0) if isinstance(result, dict) else result
        updated = result.get("updated", 0) if isinstance(result, dict) else 0
        parts = []
        if added: parts.append(f"{added} added")
        if updated: parts.append(f"{updated} updated")
        message = "Import successful" + (f": {', '.join(parts)}" if parts else "") + "."
        invalidate_scoped_cache(*cache_invalidators)
        flash(message, "success")
        return redirect(url_for("students_list"))

    @app.route("/students/import-vcf", methods=["POST"])
    @require_login
    def students_import_vcf():
        file = request.files.get("vcffile")
        if not file or file.filename == "":
            flash("No VCF file selected.", "danger")
            return redirect(url_for("students_list"))

        filename = str(file.filename or '')
        if not filename.lower().endswith('.vcf'):
            flash("Please select a .vcf contact file.", "danger")
            return redirect(url_for("students_list"))

        raw_bytes = file.read()
        if not raw_bytes:
            flash("Selected VCF file is empty.", "danger")
            return redirect(url_for("students_list"))

        try:
            vcf_text = raw_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            vcf_text = raw_bytes.decode('latin-1', errors='replace')

        contacts = _parse_vcf_contacts(vcf_text)
        if not contacts:
            flash("No valid contacts found in VCF file.", "warning")
            return redirect(url_for("students_list"))

        backup_path = db_backup_recovery.create_backup("students_import_vcf")
        cache_invalidators = [
            lambda: _invalidate_student_caches()
        ]

        added = 0
        updated = 0
        unchanged = 0
        try:
            for contact in contacts:
                result = student_manager.upsert_student_from_vcf_contact(
                    student_name=contact.get('student_name', ''),
                    email=contact.get('email', ''),
                    phone=contact.get('phone', ''),
                    guardian=contact.get('guardian', ''),
                    student_identifier=contact.get('student_identifier', ''),
                    subjects=contact.get('subjects', []),
                    match_on_email=bool(contact.get('match_on_email', True)),
                    match_on_identifier=bool(contact.get('match_on_identifier', True)),
                )
                action = result.get('action') if isinstance(result, dict) else ''
                if action == 'added':
                    added += 1
                elif action == 'updated':
                    updated += 1
                else:
                    unchanged += 1
        except Exception as e:
            flash_scoped_failure(
                backup_path=backup_path,
                table_names=("students",),
                error=e,
                invalidators=cache_invalidators,
                category="danger",
            )
            flash("VCF import failed. Please verify the contact file and try again.", "danger")
            return redirect(url_for("students_list"))

        invalidate_scoped_cache(*cache_invalidators)
        parts = []
        if added:
            parts.append(f"{added} added")
        if updated:
            parts.append(f"{updated} updated")
        if unchanged:
            parts.append(f"{unchanged} unchanged")
        summary = ', '.join(parts) if parts else 'no changes'
        flash(f"VCF import successful: {summary}.", "success")
        return redirect(url_for("students_list"))

    @app.route("/students/export")
    @require_login
    def students_export():
        export_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="students_export_", suffix=".csv", delete=False) as tmp:
                export_path = tmp.name

            # Export active students to a writable temp location.
            student_manager.export_csv(export_path)

            @after_this_request
            def _cleanup_export_file(response):
                try:
                    if export_path and os.path.exists(export_path):
                        os.remove(export_path)
                except OSError:
                    pass
                return response

            return send_file(
                export_path,
                as_attachment=True,
                download_name="students_export.csv",
                mimetype="text/csv",
            )
        except Exception as e:
            current_app.logger.exception("Student CSV export failed: %s", e)
            flash("CSV export failed. Please try again.", "danger")
            return redirect(url_for("students_list"))

    @app.route("/students/export-mailing-list")
    @require_login
    @require_feature(auth_manager.FEATURE_STUDENT_DATABASE)
    def students_export_mailing_list():
        """Export active student mailing list (name + email)."""
        rows = student_manager.get_students_mailing_list_rows()
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["name", "email"])
        for name, email in rows:
            writer.writerow([str(name or ""), str(email or "")])

        payload = BytesIO(output.getvalue().encode("utf-8"))
        return send_file(
            payload,
            as_attachment=True,
            download_name="students_mailing_list.csv",
            mimetype="text/csv",
        )

    @app.route('/students/badges/pdf')
    @require_login
    @require_feature(auth_manager.FEATURE_STUDENT_DATABASE)
    def students_badges_pdf():
        """Generate student badge cards (A4 landscape, 3.5x2in business cards).

        Optional query param:
        - student_ids: comma-separated ids (e.g. 1,2,3)
        """
        students = student_manager.get_students_badge_payload()

        raw_ids = str(request.args.get('student_ids', '') or '').strip()
        if raw_ids:
            try:
                requested_ids = [int(token) for token in raw_ids.split(',') if str(token).strip()]
            except ValueError:
                return "Invalid student selection", 400

            seen = set()
            selected_ids = []
            for sid in requested_ids:
                if sid in seen:
                    continue
                seen.add(sid)
                selected_ids.append(sid)

            if not selected_ids:
                return "No students selected", 400

            selected_set = set(selected_ids)
            students = [row for row in students if int(row[0]) in selected_set]

        if not students:
            flash("No active students found for badge printing.", "warning")
            return redirect(url_for("students_list"))

        enriched = []
        for row in students:
            sid = row[0]
            name = row[1]
            qr_blob = row[5]
            if isinstance(qr_blob, memoryview):
                qr_blob = qr_blob.tobytes()
            if not qr_blob:
                try:
                    qr_data = f"ID:{sid}\nName:{name}"
                    qr_blob = student_manager.get_student_qr_code(sid)
                    if not qr_blob:
                        from modules import qr_generator
                        qr_blob = qr_generator.generate_qr_bytes(qr_data)
                        student_manager.set_student_qr_code(sid, qr_blob)
                except Exception:
                    qr_blob = None
            enriched.append((row[0], row[1], row[2], row[3], row[4], qr_blob))

        pdf_buffer = _build_student_badges_pdf(enriched)
        return send_file(
            pdf_buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name='students_badges_a4_landscape_business_cards.pdf',
        )

    @app.route("/students/export-vcf/<int:sid>")
    @require_login
    def students_export_vcf(sid):
        """Download one student contact card in VCF format."""
        student = student_manager.get_student(sid)
        if not student:
            abort(404)

        vcf_payload = _build_student_vcf(student)
        student_name = str(student[1] or 'student').strip() if len(student) > 1 else 'student'
        suggested_name = secure_filename(student_name) or f"student_{sid}"
        download_name = f"{suggested_name}_contact.vcf"

        return send_file(
            BytesIO(vcf_payload.encode('utf-8')),
            mimetype='text/vcard; charset=utf-8',
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )
