# routes/students.py
from io import BytesIO

from flask import abort, jsonify, render_template, request, redirect, url_for, flash, send_file, current_app, after_this_request
from werkzeug.utils import secure_filename
from modules import student_manager, instructor_profile_manager, server_cache, db_backup_recovery, auth_manager
from routes.auth import require_login, require_admin
from routes.operation_utils import flash_scoped_failure, invalidate_scoped_cache
import os
import tempfile
import sqlite3
from modules.database import DB_PATH
import json
_ALLOWED_PHOTO_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_SUBJECTS = 3
MAX_SCHEDULE_DAYS = 6


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


def register_student_routes(app, upload_folder):
    """Register student CRUD and CSV routes."""
    
    @app.route("/students")
    @require_login
    def students_list():
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
            recipient_email = str(row[3] or "").strip()
            guardian = str(row[22] or "").strip() if len(row) > 22 else ""

            if not recipient_email or "@" not in recipient_email:
                skipped.append(student_name)
                continue

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
                sent.append(student_name)
            else:
                failed.append(f"{student_name} ({result.get('error', 'unknown error')})")

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
