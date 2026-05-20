# routes/api.py
from flask import jsonify, request
from modules import student_manager, assistant_manager, timer_manager, auth_manager, license_manager
from modules import server_cache
from modules.email_manager import get_email_manager, render_branded_email_shell, resolve_center_name
from modules import instructor_profile_manager
from modules.database import DB_PATH
from modules.utils import duration_seconds, time_now
from datetime import datetime
import base64
import sqlite3
import json
import traceback
from routes.auth import require_login, require_admin, require_feature

# Global helper cache for performance (UI helpers)


def _trace_column3(event: str, **fields) -> None:
    """Lightweight terminal trace for checked-out column debugging."""
    if fields:
        details = " ".join(f"{key}={fields[key]!r}" for key in sorted(fields))
        print(f"[column3-trace] {event} {details}")
    else:
        print(f"[column3-trace] {event}")


def _trace_staff_duty(event: str, **fields) -> None:
    """Terminal trace helper for Staff on Duty modal/list/toggle flows."""
    if fields:
        details = " ".join(f"{key}={fields[key]!r}" for key in sorted(fields))
        print(f"[staff-duty-trace] {event} {details}")
    else:
        print(f"[staff-duty-trace] {event}")


def _students_list_cache_key() -> str:
    return server_cache.STUDENTS_LIST_CACHE_KEY


def _has_photo_blob(student_row) -> bool:
    """Return True when the student row contains a non-empty photo blob."""
    blob, _ = _extract_photo_blob_and_mime(student_row)
    return bool(blob)


def _extract_photo_blob_and_mime(student_row):
    """Return (photo_blob_bytes|None, photo_mime) from heterogeneous student row shapes."""
    if not student_row:
        return None, ''

    # Known row shapes used across the app:
    # - get_student():      ... photo_blob(19), photo_mime(20), ...
    # - get_all_students(): ... total_study_minutes(20), photo_blob(21), photo_mime(22)
    candidate_pairs = [(21, 22), (19, 20), (20, 21)]

    def _as_blob(value):
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, (bytes, bytearray)) and len(value) > 0:
            return bytes(value)
        return None

    for blob_idx, mime_idx in candidate_pairs:
        if len(student_row) <= blob_idx:
            continue
        blob = _as_blob(student_row[blob_idx])
        if not blob:
            continue
        mime = ''
        if len(student_row) > mime_idx:
            mime = str(student_row[mime_idx] or '').strip()
        return blob, mime

    # Fallback for legacy/unknown tuple layouts: scan right-to-left for first non-empty bytes-like value.
    for idx in range(len(student_row) - 1, -1, -1):
        blob = _as_blob(student_row[idx])
        if not blob:
            continue
        mime = ''
        if idx + 1 < len(student_row):
            next_val = student_row[idx + 1]
            if isinstance(next_val, str):
                candidate_mime = next_val.strip()
                if '/' in candidate_mime:
                    mime = candidate_mime
        return blob, mime

    return None, ''


def _photo_data_uri(student_row) -> str:
    """Return a data URI for a student blob photo, or empty string if absent."""
    blob, mime = _extract_photo_blob_and_mime(student_row)
    if not blob:
        return ''
    mime = mime or 'image/png'
    encoded = base64.b64encode(blob).decode('ascii')
    return f'data:{mime};base64,{encoded}'

def _assistants_profile_cache_key() -> str:
    return server_cache.ASSISTANTS_PROFILE_LIST_CACHE_KEY

def _assistants_duty_cache_key() -> str:
    return server_cache.ASSISTANTS_DUTY_LIST_CACHE_KEY


def _subjects_from_student_row(student_row) -> list:
    """Return a safe subjects list from get_all_students() rows."""
    if not student_row:
        return []

    # Current get_all_students() shape stores subjects_json at index 18.
    raw_subjects = student_row[18] if len(student_row) > 18 else None
    if raw_subjects:
        try:
            parsed = json.loads(raw_subjects)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item or '').strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            # Legacy/malformed values can appear as delimited text; normalize gracefully.
            text = str(raw_subjects).strip()
            for sep in ('|', ';', ','):
                if sep in text:
                    tokens = [piece.strip() for piece in text.split(sep) if piece and piece.strip()]
                    if tokens:
                        return tokens

            if text:
                return [text]

    return [student_row[2]] if len(student_row) > 2 and student_row[2] else []


def _total_study_minutes_from_student_row(student_row) -> int:
    """Return total planned study minutes for a student row from get_all_students()."""
    if not student_row:
        return 30

    try:
        total_minutes = int(student_row[20]) if len(student_row) > 20 and student_row[20] is not None else 0
    except (TypeError, ValueError):
        total_minutes = 0
    if total_minutes > 0:
        return total_minutes

    # Fallback: sum subject_minutes_json when total column is missing/invalid.
    raw_minutes = student_row[19] if len(student_row) > 19 else None
    if raw_minutes:
        try:
            parsed = json.loads(raw_minutes)
            if isinstance(parsed, list):
                minute_values = [max(0, int(item)) for item in parsed if item is not None]
                summed = sum(minute_values)
                if summed > 0:
                    return summed
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    # Final fallback: subjects count * 30 minutes.
    subjects = _subjects_from_student_row(student_row)
    return max(30, len(subjects) * 30)

def _format_checkout_timestamp(value: str) -> str:
    """Format ISO-ish timestamps for human-readable emails."""
    if not value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d %I:%M:%S %p")
    except Exception:
        return str(value)


def _send_checkout_email(student_row, start_time: str, end_time: str):
    """Send checkout notification email to the student's email on file (best effort).

    Mirrors the email_manager.send_email() pattern used by the utilities
    Student Activity Card send-email route.

    Returns a dict:
            - status: sent | disabled | no_email | failed | error
      - message: human-readable short message
    """
    import traceback as _tb

    try:
        if not student_row:
            return {"status": "error", "message": "Student not found for checkout email"}

        student_name = student_row[1] if len(student_row) > 1 else "Student"
        guardian_name = str(student_row[22] or '').strip() if len(student_row) > 22 else ''
        checkout_notify_enabled = bool(student_row[24]) if len(student_row) > 24 else True

        if not checkout_notify_enabled:
            print(f"[checkout-email] Skipped for {student_name}: checkout notifications disabled")
            return {"status": "disabled", "message": "Checkout notification disabled for this student"}

        recipient_email = (student_row[3] if len(student_row) > 3 else "") or ""
        recipient_email = recipient_email.strip()

        if not recipient_email or "@" not in recipient_email:
            print(f"[checkout-email] Skipped for {student_name}: no valid email on file")
            return {"status": "no_email", "message": "No email on file"}

        start_display = _format_checkout_timestamp(start_time)
        end_display = _format_checkout_timestamp(end_time)

        try:
            total_seconds = max(0, int(duration_seconds(start_time, end_time)))
        except Exception:
            total_seconds = 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        duration_display = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        profile = instructor_profile_manager.get_instructor_profile() or {}
        center_name = str(profile.get('center_location') or '').strip() or resolve_center_name()
        salutation = f"Dear {guardian_name}," if guardian_name else "Dear Parent/Guardian,"

        email_subject = f"{center_name} - Class Checkout - {student_name}"

        body = (
            f"{salutation}\n\n"
            f"{student_name} has checked out from class.\n\n"
            f"Guardian:       {guardian_name or 'Parent/Guardian'}\n"
            f"Start Time:       {start_display}\n"
            f"End Time:         {end_display}\n"
            f"Session Duration: {duration_display}\n\n"
            f"Center: {center_name}\n\n"
            f"This is an automated message. Please do not reply."
        )

        html_body = render_branded_email_shell(
            title=f"{center_name} Class Checkout Confirmation",
            center_name=center_name,
            subtitle=center_name,
            footer_note=f"This is an automated checkout message from {center_name}. Please do not reply to this email.",
            body_html=f"""
                <p>{salutation}</p>
                <div class="highlight"><strong>{student_name}</strong> has checked out from class.</div>
                <table class="report-table">
                    <tr><th>Guardian</th><td>{guardian_name or 'Parent/Guardian'}</td></tr>
                    <tr><th>Start Time</th><td>{start_display}</td></tr>
                    <tr><th>End Time</th><td>{end_display}</td></tr>
                    <tr><th>Session Duration</th><td>{duration_display}</td></tr>
                    <tr><th>Center</th><td>{center_name}</td></tr>
                </table>
            """
        )

        # Use the same email_manager pattern as utilities/report-card/send-email
        email_manager = get_email_manager()
        result = email_manager.send_email(
            recipient_email=recipient_email,
            subject=email_subject,
            body=body,
            html_body=html_body,
        )
        if result.get('success', False):
            print(f"[checkout-email] Sent to {recipient_email} for {student_name}")
            return {"status": "sent", "message": "Checkout email sent"}
        else:
            failure_reason = result.get('error') or 'Unknown email error'
            print(f"[checkout-email] Failed for {student_name}: {failure_reason}")
            return {"status": "failed", "message": f"Checkout email failed: {failure_reason}"}

    except Exception as e:
        print(f"[checkout-email] Unexpected error for student: {e}\n{_tb.format_exc()}")
        return {"status": "error", "message": f"Checkout email error: {e}"}


def register_api_routes(app):
    """Register API/AJAX routes."""
    
    @app.route("/api/students/list")
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_students_list():
        """Return students with computed status: registered | active | checked."""
        cache_key = _students_list_cache_key()

        def _build_students_list_payload():
            students = student_manager.get_all_students()

            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                active_rows = c.execute(
                    """
                    SELECT student_id, start_time
                    FROM sessions
                    WHERE end_time IS NULL
                    """,
                    (),
                ).fetchall()
                active_map = {sid: start for sid, start in active_rows}

                today = datetime.now().date().isoformat()
                today_rows = c.execute(
                    """
                    SELECT student_id, SUM(duration)
                    FROM sessions
                    WHERE DATE(start_time)=?
                      AND end_time IS NOT NULL
                    GROUP BY student_id
                    """,
                    (today,),
                ).fetchall()

                today_sum = {sid: secs for sid, secs in today_rows}

                latest_rows = c.execute(
                    """
                    SELECT student_id, duration
                    FROM sessions
                    WHERE end_time IS NOT NULL
                    ORDER BY id DESC
                    """,
                    (),
                ).fetchall()
                latest_duration = {}
                for sid, dur in latest_rows:
                    if sid not in latest_duration:
                        latest_duration[sid] = dur

            _trace_column3(
                "students_list_build_source",
                total_students=len(students),
                active_rows=len(active_rows),
                checked_rows=len(today_rows),
                latest_rows=len(latest_rows),
                today=today,
            )

            result = []
            checked_ids = []
            for s in students:
                sid = s[0]
                status = "registered"
                start_time = None
                total_seconds = None
                dur = latest_duration.get(sid)

                if sid in active_map:
                    status = "active"
                    start_time = active_map[sid]
                elif sid in today_sum:
                    status = "checked"
                    total_seconds = today_sum.get(sid, 0)
                    checked_ids.append(sid)

                student_dict = {
                    "id": sid,
                    "name": s[1],
                    "subject": s[2],
                    "level": s[3],
                    "email": s[4],
                    "phone": s[5],
                    "guardian": s[6] if len(s) > 6 else '',
                    "active": s[8] if len(s) > 8 else 0,
                    "book_loaned": s[9] if len(s) > 9 else 0,
                    "device_loaned": s[10] if len(s) > 10 else 0,
                    "day1": s[14] if len(s) > 14 else None,
                    "day1_time": s[15] if len(s) > 15 else None,
                    "day2": s[16] if len(s) > 16 else None,
                    "day2_time": s[17] if len(s) > 17 else None,
                    "subjects": _subjects_from_student_row(s),
                    "status": status,
                    "start_time": start_time,
                    "total_seconds": total_seconds,
                    "duration": dur,
                    "photo_url": f"/students/photo/{sid}" if _has_photo_blob(s) else "",
                }
                result.append(student_dict)

            _trace_column3(
                "students_list_payload_built",
                cache_key=cache_key,
                checked_ids=checked_ids,
                checked_count=len(checked_ids),
            )
            return result

        result = server_cache.get_or_set(
            cache_key,
            _build_students_list_payload,
            policy="checkin",
        )

        checked_count = sum(1 for student in result if student.get("status") == "checked")
        _trace_column3(
            "students_list_response",
            cache_key=cache_key,
            total=len(result),
            checked_count=checked_count,
        )

        return jsonify(result)

    @app.route("/api/students/start/<int:sid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_students_start(sid):
        student = student_manager.get_student(sid)
        if not student:
            return jsonify({"error": "Student not found"}), 404
        timer_manager.start_session(sid)
        server_cache.invalidate(_students_list_cache_key())
        return jsonify({"status": "started"})

    @app.route("/api/students/stop/<int:sid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_students_stop(sid):
        student = student_manager.get_student(sid)
        if not student:
            return jsonify({"error": "Student not found"}), 404
        _trace_column3("checkout_begin", sid=sid, student_name=student[1])
        checkout_email_status = None
        checkout_email_message = None
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            open_row = c.execute(
                """
                SELECT id, start_time
                FROM sessions
                WHERE student_id = ?
                  AND end_time IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (sid,),
            ).fetchone()
            if open_row:
                sess_id, start = open_row
                end = time_now()
                try:
                    duration = duration_seconds(start, end)
                except Exception:
                    duration = 0
                c.execute(
                    "UPDATE sessions SET end_time = ?, duration = ? WHERE id = ?",
                    (end, duration, sess_id),
                )
                conn.commit()
                _trace_column3(
                    "checkout_db_updated",
                    sid=sid,
                    sess_id=sess_id,
                    duration=duration,
                    start=start,
                    end=end,
                )
                email_result = _send_checkout_email(student, start, end) or {}
                checkout_email_status = email_result.get("status")
                checkout_email_message = email_result.get("message")
        cache_key = _students_list_cache_key()
        server_cache.invalidate(cache_key)
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            today = datetime.now().date().isoformat()
            checked_total = c.execute(
                """
                SELECT COUNT(DISTINCT student_id)
                FROM sessions
                WHERE DATE(start_time)=?
                  AND end_time IS NOT NULL
                """,
                (today,),
            ).fetchone()[0] or 0
        _trace_column3(
            "checkout_complete",
            sid=sid,
            cache_key=cache_key,
            checkout_email_status=checkout_email_status,
            checked_total=checked_total,
        )
        return jsonify({
            "status": "stopped",
            "checkout_email_status": checkout_email_status,
            "checkout_email_message": checkout_email_message,
        })

    @app.route("/api/sessions/active")
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_sessions_active():
        """Return only currently active sessions; auto-stop any over 2h."""
        now_str = time_now()
        today = datetime.now().date().isoformat()

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            active_rows = c.execute(
                """
                SELECT student_id, start_time
                FROM sessions
                WHERE end_time IS NULL
                  AND DATE(start_time)=?
                """,
                (today,),
            ).fetchall()

        for sid, start in list(active_rows):
            try:
                if duration_seconds(start, now_str) >= 7200:
                    with sqlite3.connect(DB_PATH) as conn:
                        c = conn.cursor()
                        end = time_now()
                        try:
                            duration = duration_seconds(start, end)
                        except Exception:
                            duration = 0
                        c.execute(
                            """
                            UPDATE sessions
                            SET end_time = ?, duration = ?
                            WHERE id = (
                                SELECT id
                                FROM sessions
                                WHERE student_id = ?
                                  AND end_time IS NULL
                                ORDER BY id DESC
                                LIMIT 1
                            )
                            """,
                            (end, duration, sid),
                        )
                        conn.commit()
                    _trace_column3(
                        "active_session_auto_closed",
                        sid=sid,
                        duration=duration,
                        start=start,
                        end=end,
                    )
            except Exception:
                continue

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            active_rows = c.execute(
                """
                SELECT student_id, start_time
                FROM sessions
                WHERE end_time IS NULL
                  AND DATE(start_time)=?
                """,
                (today,),
            ).fetchall()

        students = {s[0]: s for s in student_manager.get_all_students()}
        result = []
        for sid, start in active_rows:
            s = students.get(sid)
            if not s:
                continue
            result.append({
                "id": sid,
                "name": s[1],
                "subject": s[2],
                "level": s[3],
                "book_loaned": s[8] if len(s) > 8 else 0,
                "device_loaned": s[9] if len(s) > 9 else 0,
                "start_time": start,
                "subjects": _subjects_from_student_row(s),
                "total_study_minutes": _total_study_minutes_from_student_row(s),
                "photo_url": f"/students/photo/{sid}" if _has_photo_blob(s) else '',
                "photo_data_uri": _photo_data_uri(s),
            })

        return jsonify(result)

    @app.route("/api/sessions/clear", methods=["POST"])
    @require_admin
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_sessions_clear():
        """Stop all active sessions (DB + cache) and clear timer buffers."""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute("DELETE FROM sessions")
                closed_rows = c.rowcount
                conn.commit()
            ended = []
            server_cache.invalidate(_students_list_cache_key())
            return jsonify({"stopped": ended, "closed_rows": closed_rows}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/sessions/toggle", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def api_sessions_toggle():
        """Toggle a student's session: start if not active, stop if active.
        Request JSON: {"student_id": <id>}
        Returns: {"action": "started"|"checked_out", "student_id": <id>, "name": <name>}
        """
        try:
            data = request.get_json() or {}
            student_id = data.get("student_id")
            
            if not student_id:
                return jsonify({"error": "Missing student_id"}), 400
            
            # Get student info
            student = student_manager.get_student(student_id)
            if not student:
                return jsonify({"error": "Student not found"}), 404
            
            student_name = student[1]  # name is at index 1
            
            # Check if student has an open session
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                open_session = c.execute(
                    "SELECT id FROM sessions WHERE student_id=? AND end_time IS NULL LIMIT 1",
                    (student_id,)
                ).fetchone()
            
            if open_session:
                # Stop the session (check out)
                checkout_email_status = None
                checkout_email_message = None
                with sqlite3.connect(DB_PATH) as conn:
                    c = conn.cursor()
                    open_row = c.execute(
                        """
                        SELECT id, start_time
                        FROM sessions
                        WHERE student_id = ?
                          AND end_time IS NULL
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (student_id,),
                    ).fetchone()
                    if open_row:
                        sess_id, start = open_row
                        end = time_now()
                        try:
                            duration = duration_seconds(start, end)
                        except Exception:
                            duration = 0
                        c.execute(
                            "UPDATE sessions SET end_time = ?, duration = ? WHERE id = ?",
                            (end, duration, sess_id),
                        )
                        conn.commit()
                        _trace_column3(
                            "toggle_checkout_db_updated",
                            student_id=student_id,
                            sess_id=sess_id,
                            duration=duration,
                            start=start,
                            end=end,
                        )
                        email_result = _send_checkout_email(student, start, end) or {}
                        checkout_email_status = email_result.get("status")
                        checkout_email_message = email_result.get("message")
                cache_key = _students_list_cache_key()
                server_cache.invalidate(cache_key)
                _trace_column3(
                    "toggle_checkout_complete",
                    student_id=student_id,
                    cache_key=cache_key,
                    checkout_email_status=checkout_email_status,
                )
                return jsonify({
                    "action": "checked_out",
                    "student_id": student_id,
                    "name": student_name,
                    "checkout_email_status": checkout_email_status,
                    "checkout_email_message": checkout_email_message,
                }), 200
            else:
                # Start a new session
                timer_manager.start_session(student_id)
                server_cache.invalidate(_students_list_cache_key())
                return jsonify({
                    "action": "started",
                    "student_id": student_id,
                    "name": student_name
                }), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/attendance/reset_today", methods=["POST"])
    @require_admin
    def api_attendance_reset_today():
        """Reset today's attendance data and clear any active class timers.
        - Stops all active sessions
        - Deletes sessions whose start_time is today
        - Clears active cache for dashboard columns
        """
        # Stop any active timers first
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            open_rows = c.execute(
                "SELECT id, student_id, start_time FROM sessions WHERE end_time IS NULL",
                ()
            ).fetchall()
            end = time_now()
            for sess_id, sid, start in open_rows:
                try:
                    duration = duration_seconds(start, end)
                except Exception:
                    duration = 0
                c.execute(
                    "UPDATE sessions SET end_time = ?, duration = ? WHERE id = ?",
                    (end, duration, sess_id),
                )
            conn.commit()

        today = datetime.now().date().isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM sessions WHERE DATE(start_time)=?", (today))
            deleted = c.rowcount
            conn.commit()

        server_cache.invalidate(_students_list_cache_key())
        return jsonify({"deleted": deleted, "date": today})

    @app.route("/api/assistants/profiles")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_profiles():
        """Return assistant static profile list with longer TTL lane."""
        def _build_profiles_payload():
            rows = assistant_manager.get_all_assistants()
            return [
                dict(
                    id=a[0],
                    name=a[1],
                    role=a[2] if len(a) > 2 else "",
                    email=a[3] if len(a) > 3 else "",
                    phone=a[4] if len(a) > 4 else "",
                )
                for a in rows
            ]

        payload = server_cache.get_or_set(
            _assistants_profile_cache_key(),
            _build_profiles_payload,
            policy="assistant_profile",
        )
        return jsonify(payload)

    @app.route("/api/assistants/list")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_list():
        """Return all assistants with on-duty status and start time.
        DB is the source of truth: an "open" assistant_sessions row (end_time NULL) => on duty.
        """
        _trace_staff_duty("list_request", method=request.method, path=request.path)

        def _build_duty_payload():
            _trace_staff_duty("list_build_begin")
            assistants = assistant_manager.get_all_assistants()
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                try:
                    open_rows = c.execute(
                        "SELECT assistant_id, start_time FROM assistant_sessions WHERE end_time IS NULL",
                        (),
                    ).fetchall()
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "no such table" in msg and "assistant_sessions" in msg:
                        _trace_staff_duty("list_missing_table_autocreate")
                        c.execute(
                            """
                            CREATE TABLE IF NOT EXISTS assistant_sessions (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                assistant_id INTEGER,
                                start_time TEXT,
                                end_time TEXT,
                                duration INTEGER,
                                FOREIGN KEY(assistant_id) REFERENCES staff(id)
                            )
                            """,
                            (),
                        )
                        conn.commit()
                        open_rows = []
                    else:
                        raise
            _trace_staff_duty(
                "list_build_source",
                assistants_count=len(assistants),
                open_rows_count=len(open_rows),
            )
            open_map = {aid: start for (aid, start) in open_rows}
            result = []
            for a in assistants:
                aid = a[0]
                result.append(
                    dict(
                        id=aid,
                        name=a[1],
                        role=a[2] if len(a) > 2 else "",
                        email=a[3] if len(a) > 3 else "",
                        phone=a[4] if len(a) > 4 else "",
                        on_duty=aid in open_map,
                        start_time=open_map.get(aid),
                    )
                )
            _trace_staff_duty("list_build_done", payload_count=len(result))
            return result

        try:
            payload = server_cache.get_or_set(
                _assistants_duty_cache_key(),
                _build_duty_payload,
                policy="assistant_duty",
            )
            _trace_staff_duty("list_response_ok", payload_type=type(payload).__name__, payload_count=(len(payload) if isinstance(payload, list) else -1))
            return jsonify(payload)
        except Exception as e:
            _trace_staff_duty("list_response_error", error=str(e))
            print(traceback.format_exc())
            return jsonify({"error": f"Staff list failed: {e}"}), 500

    @app.route("/api/assistants/select/<int:aid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_assistants_select(aid):
        """Toggle assistant on/off duty with payroll time tracking.
        Uses DB open-row semantics so checkout works reliably (even after restarts).
        """
        _trace_staff_duty("select_request", aid=aid, method=request.method, path=request.path)
        try:
            assistant = assistant_manager.get_assistant(aid)
            if not assistant:
                _trace_staff_duty("select_not_found", aid=aid)
                return jsonify({"error": "Staff member not found"}), 404

            now = datetime.now()
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                open_row = cur.execute(
                    "SELECT id, start_time FROM assistant_sessions WHERE assistant_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
                    (aid,),
                ).fetchone()

                _trace_staff_duty("select_open_row", aid=aid, has_open_row=bool(open_row))

                if open_row:
                    sess_id, start_iso = open_row
                    try:
                        start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                    except Exception:
                        start_dt = None
                    duration = int((now - start_dt).total_seconds()) if start_dt else 0
                    cur.execute(
                        "UPDATE assistant_sessions SET end_time=?, duration=? WHERE id=?",
                        (now.isoformat(), duration, sess_id),
                    )
                    conn.commit()
                    server_cache.invalidate(_assistants_duty_cache_key())
                    _trace_staff_duty("select_checkout_ok", aid=aid, sess_id=sess_id, duration=duration)
                    return jsonify({"success": True, "on_duty": False, "duration": duration})
                else:
                    # Start new open session
                    cur.execute(
                        "INSERT INTO assistant_sessions (assistant_id, start_time, end_time, duration) VALUES (?, ?, NULL, NULL)",
                        (aid, now.isoformat()),
                    )
                    conn.commit()
                    server_cache.invalidate(_assistants_duty_cache_key())
                    _trace_staff_duty("select_checkin_ok", aid=aid)
                    return jsonify({"success": True, "on_duty": True})
        except Exception as e:
            _trace_staff_duty("select_error", aid=aid, error=str(e))
            print(traceback.format_exc())
            return jsonify({"error": f"Staff toggle failed: {e}"}), 500
