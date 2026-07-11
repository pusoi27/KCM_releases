import calendar
import io
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import flash, redirect, render_template, request, session, url_for
from flask_wtf.csrf import ValidationError, validate_csrf

from modules import instructor_profile_manager, student_manager
from modules.database import DB_PATH
from modules.email_manager import get_email_manager, render_branded_email_shell, resolve_center_name
from routes.auth import require_login
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


def _parse_notice_date(raw_value: str) -> date:
    token = str(raw_value or "").strip()
    if not token:
        return date.today()
    try:
        return date.fromisoformat(token)
    except ValueError:
        return date.today()


def _parse_optional_iso_date(raw_value: str) -> date | None:
    token = str(raw_value or "").strip()
    if not token:
        return None
    try:
        return date.fromisoformat(token)
    except ValueError:
        return None


def _last_day_of_next_month(notice_date: date) -> date:
    year = notice_date.year
    month = notice_date.month + 1
    if month > 12:
        month = 1
        year += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _effective_last_attendance_date(notice_date: date) -> date:
    """Calculate effective last attendance date.

    Policy:
    - flag = current day + 30
    - if flag == 31 and current month has 31 days, use last day of current month
    - if flag > 31, use last day of next month
    - otherwise, use notice date + 30 days
    """
    flag = int(notice_date.day) + 30
    current_month_last_day = calendar.monthrange(notice_date.year, notice_date.month)[1]

    if flag == 31 and current_month_last_day == 31:
        return date(notice_date.year, notice_date.month, current_month_last_day)

    if flag > 31:
        return _last_day_of_next_month(notice_date)

    return notice_date + timedelta(days=30)


def _calculate_last_payment_date(notice_date: date) -> tuple[date | None, bool]:
    """Calculate last payment date.

    Policy:
    - Add 30 days to notice date
    - If result is in next month, last payment date is 1st of next month
    - If result is still in current month, no payment required (return None)

    Returns: (payment_date or None, requires_payment)
    """
    candidate = notice_date + timedelta(days=30)
    
    # If candidate month is different from notice month, payment needed for 1st of next month
    if candidate.month != notice_date.month or candidate.year != notice_date.year:
        # Result is in next month
        next_month_first = date(candidate.year, candidate.month, 1)
        return (next_month_first, True)
    else:
        # Result is still in current month - no payment required
        return (None, False)


def _format_ordinal_day(day: int) -> str:
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _format_long_date(value: date | None) -> str:
    if not value:
        return ""
    return f"{value.strftime('%B')} {_format_ordinal_day(value.day)}, {value.year}"


def _send_cancellation_confirmation_email(
    guardian_email: str,
    guardian_name: str,
    student_name: str,
    notice_date: date,
    last_attendance_date: date,
    last_payment_date: date | None,
    center_name: str,
    center_phone: str,
    center_email: str,
) -> dict:
    """Send enrollment cancellation confirmation email to guardian."""
    email_manager = get_email_manager()

    notice_text = _format_long_date(notice_date)
    attendance_text = _format_long_date(last_attendance_date)
    payment_text = _format_long_date(last_payment_date) if last_payment_date else "No additional payment required"

    subject = f"Enrollment Cancellation Confirmation - {student_name}"

    plain_body = (
        f"Dear {guardian_name},\n\n"
        f"We have received your cancellation notice for {student_name} on {notice_text}. "
        f"The cancellation will be processed as follows:\n\n"
        f"- Last day of attendance: {attendance_text}\n"
        f"- Last payment date: {payment_text}\n\n"
        f"Please keep a copy of this email for your records. If you have any questions or concerns, please contact your center at {center_phone} or {center_email}.\n\n"
        f"Thank you for being a valued part of our family at {center_name}. We wish {student_name} success and the best grades in school!\n"
    )

    html_body = render_branded_email_shell(
        title="Enrollment Cancellation Confirmation",
        center_name=center_name,
        subtitle="Cancellation Processed",
        footer_note=f"This is an automated confirmation from {center_name}.",
        body_html=(
            f"<p>Dear {guardian_name},</p>"
            f"<p>We have received your cancellation notice for <strong>{student_name}</strong> on "
            f"{notice_text}. The cancellation will be processed as follows:</p>"
            f"<table class='report-table'>"
            f"<tr><th>Last day of attendance</th><td>{attendance_text}</td></tr>"
            f"<tr><th>Last payment date</th><td>{payment_text}</td></tr>"
            f"</table>"
            f"<p>Please keep a copy of this email for your records. If you have any questions or concerns, please contact your center at <strong>{center_phone}</strong> or <strong>{center_email}</strong>.</p>"
            f"<p>Thank you for being a valued part of our family at {center_name}. We wish {student_name} success and the best grades in school!</p>"
        ),
    )

    return email_manager.send_email(
        recipient_email=guardian_email,
        subject=subject,
        body=plain_body,
        html_body=html_body,
        no_reply=False,
    )


def _safe_student_rows():
    rows = student_manager.get_student_database_rows(active=1)
    items = []
    for row in rows:
        items.append(
            {
                "id": int(row.get("id")),
                "name": str(row.get("name") or "").strip(),
                "email": str(row.get("email") or "").strip(),
                "phone": str(row.get("phone") or "").strip(),
                "guardian": str(row.get("guardian") or "").strip(),
            }
        )
    return sorted(items, key=lambda x: x["name"].lower())


def _build_selection_options(student_rows: list[dict]):
    options = []
    option_map = {}

    for student in student_rows:
        token = f"student:{student['id']}"
        label = f"{student['name']}"
        if student.get("guardian"):
            label = f"{label} ({student['guardian']})"
        options.append({"value": token, "label": label})
        option_map[token] = [student["id"]]

    return options, option_map


def _selected_students(student_rows: list[dict], selected_ids: list[int]) -> list[dict]:
    selected_set = set(int(sid) for sid in selected_ids)
    return [s for s in student_rows if int(s["id"]) in selected_set]


def _insert_cancellation_notice(
    student_ids: list[int],
    student_names: list[str],
    selection_label: str,
    notice_date: date,
    effective_last_attendance_date: date,
    customer_name: str,
    customer_email: str,
    center_email_sent: bool,
    center_email_status: str,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cancellation_notices (
                student_ids_json,
                student_names_json,
                selection_label,
                notice_date,
                effective_last_attendance_date,
                customer_name,
                customer_email,
                ack_method,
                ack_identity_confirmed,
                ack_last_day_confirmed,
                center_email_sent,
                center_email_status,
                center_email_sent_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'checkbox', 1, 1, ?, ?, ?)
            """,
            (
                json.dumps(student_ids),
                json.dumps(student_names),
                str(selection_label or "").strip(),
                notice_date.isoformat(),
                effective_last_attendance_date.isoformat(),
                str(customer_name or "").strip(),
                str(customer_email or "").strip(),
                1 if center_email_sent else 0,
                str(center_email_status or "").strip(),
                datetime.utcnow().isoformat() if center_email_sent else "",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def _build_cancellation_notice_pdf(
    student_item: dict,
    notice_date: date,
    effective_date: date,
    center_name: str,
    center_email: str,
):
    exports_dir = os.path.join(os.path.dirname(DB_PATH), "..", "exports", "cancellation_notices")
    exports_dir = os.path.abspath(exports_dir)
    os.makedirs(exports_dir, exist_ok=True)

    student_name = str(student_item.get("name") or "").strip()
    guardian_name = str(student_item.get("guardian") or "").strip() or "Parent/Guardian"
    safe_student = "_".join(student_name.split()) or f"student_{student_item.get('id', 'unknown')}"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_filename = f"cancellation_notice_{safe_student}_{stamp}.pdf"
    pdf_path = os.path.join(exports_dir, pdf_filename)

    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=letter)
    page_width, page_height = letter
    left = 0.85 * inch
    top = page_height - 0.9 * inch

    c.setFont("Helvetica-Bold", 18)
    c.drawString(left, top, "Cancellation Notice")

    y = top - 0.35 * inch
    c.setFont("Helvetica", 10)
    c.drawString(left, y, f"Center: {center_name}")
    y -= 0.2 * inch
    c.drawString(left, y, f"Center Email (forward signed form to): {center_email or 'N/A'}")

    y -= 0.35 * inch
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "Student / Customer Information")
    y -= 0.22 * inch
    c.setFont("Helvetica", 11)
    c.drawString(left, y, f"Student Name: {student_name or 'N/A'}")
    y -= 0.22 * inch
    c.drawString(left, y, f"Guardian Name: {guardian_name}")
    y -= 0.22 * inch
    c.drawString(left, y, f"Cancellation Notice Date: {notice_date.isoformat()}")
    y -= 0.22 * inch
    c.drawString(left, y, f"Effective Last Attendance Date: {effective_date.isoformat()}")

    y -= 0.35 * inch
    c.setFont("Helvetica-Bold", 12)
    c.drawString(left, y, "Acknowledgement Instructions")
    y -= 0.24 * inch
    c.setFont("Helvetica", 10)
    text = (
        "No signature is required on this PDF. This email, together with the attached pre-filled PDF, is the customer's "
        "acknowledgment. Please forward the email to the center email listed above as confirmation."
    )
    text_obj = c.beginText(left, y)
    text_obj.setFont("Helvetica", 10)
    text_obj.setLeading(14)
    for line in [text[i:i + 105] for i in range(0, len(text), 105)]:
        text_obj.textLine(line)
    c.drawText(text_obj)

    y -= 0.75 * inch
    c.setFont("Helvetica", 10)
    c.drawString(left, y, "Forward this email to the center email above to confirm receipt of the cancellation notice.")

    y -= 0.55 * inch
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(left, y, f"Generated by Stdytime on {datetime.now().isoformat(timespec='seconds')}")

    c.showPage()
    c.save()

    with open(pdf_path, "wb") as fh:
        fh.write(packet.getvalue())

    return pdf_path


def _send_customer_cancellation_notice_email(
    center_email: str,
    center_name: str,
    student_item: dict,
    notice_date: date,
    effective_date: date,
    pdf_path: str,
    customer_email: str,
):
    email_manager = get_email_manager()
    student_name = str(student_item.get("name") or "").strip() or "Student"
    guardian_name = str(student_item.get("guardian") or "").strip() or "Parent/Guardian"

    subject = f"{center_name} — Cancellation Notice for {student_name}"
    plain_body = (
        f"Dear {guardian_name},\n\n"
        f"Please review the attached prefilled cancellation notice for {student_name}.\n"
        f"Notice Date: {notice_date.isoformat()}\n"
        f"Effective Last Attendance Date: {effective_date.isoformat()}\n\n"
        f"Important: No signature is required. Please forward this email, with the attached PDF, to {center_email or 'the center email listed in the PDF'} as your acknowledgment.\n\n"
        f"This message is sent from a no-reply flow in Stdytime; replies are not monitored.\n"
    )

    html_body = render_branded_email_shell(
        title="Cancellation Notice (Prefilled PDF)",
        center_name=center_name,
        subtitle=center_name,
        footer_note=(
            f"This is an automated no-reply workflow message from {center_name}. "
            "Please forward the signed PDF to your center email as instructed."
        ),
        body_html=(
            f"<p>Dear {guardian_name},</p>"
            f"<p>Please review the attached prefilled cancellation notice for <strong>{student_name}</strong>.</p>"
            f"<table class='report-table'>"
            f"<tr><th>Notice Date</th><td>{notice_date.isoformat()}</td></tr>"
            f"<tr><th>Effective Last Attendance Date</th><td>{effective_date.isoformat()}</td></tr>"
            f"<tr><th>Forward Signed Form To</th><td>{center_email or 'Center email in attached PDF'}</td></tr>"
            f"</table>"
            f"<div class='highlight'>No signature is required. Please forward this email, with the attached PDF, to the center email for acknowledgment.</div>"
        ),
    )

    return email_manager.send_email(
        recipient_email=customer_email,
        subject=subject,
        body=plain_body,
        html_body=html_body,
        attachments=[pdf_path],
        no_reply=True,
        from_email="noreply@stdytime.com",
    )


def _insert_cancellation_notice_email_dispatch(
    student_item: dict,
    selection_label: str,
    notice_date: date,
    effective_last_attendance_date: date,
    customer_email: str,
    center_email_sent: bool,
    center_email_status: str,
):
    student_id = int(student_item.get("id"))
    student_name = str(student_item.get("name") or "").strip()
    guardian_name = str(student_item.get("guardian") or "").strip()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cancellation_notices (
                student_ids_json,
                student_names_json,
                selection_label,
                notice_date,
                effective_last_attendance_date,
                customer_name,
                customer_email,
                ack_method,
                ack_identity_confirmed,
                ack_last_day_confirmed,
                center_email_sent,
                center_email_status,
                center_email_sent_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'prefilled_pdf_email', 1, 1, ?, ?, ?)
            """,
            (
                json.dumps([student_id]),
                json.dumps([student_name]),
                str(selection_label or "").strip(),
                notice_date.isoformat(),
                effective_last_attendance_date.isoformat(),
                guardian_name,
                str(customer_email or "").strip(),
                1 if center_email_sent else 0,
                str(center_email_status or "").strip(),
                datetime.utcnow().isoformat() if center_email_sent else "",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def register_cancellation_routes(app):
    @app.route("/utilities/cancellation-notice", methods=["GET", "POST"])
    @require_login
    def cancellation_notice_page():
        student_rows = _safe_student_rows()
        options, option_map = _build_selection_options(student_rows)
        profile = instructor_profile_manager.get_instructor_profile() or {}
        center_name = resolve_center_name()
        center_email = str(profile.get("email") or "").strip()
        center_phone = str(profile.get("phone") or "").strip()
        center_location = str(profile.get("center_location") or "").strip()
        center_address = str(profile.get("center_address") or "").strip()

        # Get selection from POST data (form submission) or GET args (initial page load)
        selected_option = str(request.form.get("selection") or request.args.get("selection") or "").strip()

        notice_date = date.today()
        effective_date = _effective_last_attendance_date(notice_date)
        last_payment_date, requires_payment = _calculate_last_payment_date(notice_date)

        selected_ids = option_map.get(selected_option, [])
        selected_students = _selected_students(student_rows, selected_ids) if selected_ids else []

        email_sent = False
        email_result = None

        # Send confirmation email only when "Send Notice" button is clicked.
        # Preview submissions are intentionally CSRF-exempt because they are read-only.
        action = str(request.form.get("action") or "").strip()
        send_notice_requested = request.method == "POST" and action == "send_notice"
        if send_notice_requested:
            try:
                validate_csrf(str(request.form.get("csrf_token") or "").strip())
            except (ValidationError, ValueError):
                flash(
                    "Your preview loaded, but the send action needs a fresh security token. Please reload the page and try again.",
                    "warning",
                )
                send_notice_requested = False

        if send_notice_requested and selected_students:
            student_item = selected_students[0]
            guardian_email = str(student_item.get("email") or "").strip()
            guardian_name = str(student_item.get("guardian") or "").strip() or "Parent/Guardian"
            student_name = str(student_item.get("name") or "").strip()

            custom_last_attendance_raw = str(request.form.get("custom_last_attendance_date") or "").strip()
            custom_last_attendance = _parse_optional_iso_date(custom_last_attendance_raw)
            if custom_last_attendance_raw and custom_last_attendance:
                effective_date = custom_last_attendance
            elif custom_last_attendance_raw and not custom_last_attendance:
                flash("Invalid custom last day of attendance. Using calculated date.", "warning")

            custom_last_payment_raw = str(request.form.get("custom_last_payment_date") or "").strip()
            custom_last_payment = _parse_optional_iso_date(custom_last_payment_raw)
            if custom_last_payment_raw and custom_last_payment:
                last_payment_date = custom_last_payment
                requires_payment = True
            elif custom_last_payment_raw and not custom_last_payment:
                flash("Invalid custom last payment date. Using calculated date.", "warning")

            if guardian_email and "@" in guardian_email:
                center_phone_display = center_phone or "954-931-1541"

                email_result = _send_cancellation_confirmation_email(
                    guardian_email=guardian_email,
                    guardian_name=guardian_name,
                    student_name=student_name,
                    notice_date=notice_date,
                    last_attendance_date=effective_date,
                    last_payment_date=last_payment_date,
                    center_name=center_name,
                    center_phone=center_phone_display,
                    center_email=center_email,
                )

                if email_result.get("success"):
                    center_copy_sent = False
                    center_copy_status = ""
                    center_copy_target = str(center_email or "").strip()
                    if center_copy_target and "@" in center_copy_target and center_copy_target.lower() != guardian_email.lower():
                        center_copy_result = _send_cancellation_confirmation_email(
                            guardian_email=center_copy_target,
                            guardian_name=guardian_name,
                            student_name=student_name,
                            notice_date=notice_date,
                            last_attendance_date=effective_date,
                            last_payment_date=last_payment_date,
                            center_name=center_name,
                            center_phone=center_phone_display,
                            center_email=center_email,
                        )
                        center_copy_sent = bool(center_copy_result.get("success"))
                        if center_copy_sent:
                            center_copy_status = f" Copy sent to instructor profile email ({center_copy_target})."
                        else:
                            center_copy_status = (
                                f" Guardian email sent, but copy to instructor profile email ({center_copy_target}) failed: "
                                f"{center_copy_result.get('error', 'unknown error')}."
                            )
                    elif center_copy_target and center_copy_target.lower() == guardian_email.lower():
                        center_copy_sent = True
                        center_copy_status = " Copy skipped because guardian and instructor profile email are identical."
                    elif center_copy_target and "@" not in center_copy_target:
                        center_copy_status = " Guardian email sent, but instructor profile email is invalid for copy delivery."

                    selection_label = next((o["label"] for o in options if o["value"] == selected_option), "")
                    notice_id = _insert_cancellation_notice(
                        student_ids=[int(student_item.get("id"))],
                        student_names=[student_name],
                        selection_label=selection_label,
                        notice_date=notice_date,
                        effective_last_attendance_date=effective_date,
                        customer_name=guardian_name,
                        customer_email=guardian_email,
                        center_email_sent=center_copy_sent,
                        center_email_status=(
                            "Confirmation email sent to guardian."
                            + (center_copy_status or "")
                        ).strip(),
                    )
                    flash(
                        f"Cancellation confirmation email sent to {guardian_name} at {guardian_email} (Notice #{notice_id}). "
                        f"Last day of attendance: {_format_long_date(effective_date)}"
                        + (f"{center_copy_status}" if center_copy_status else ""),
                        "success",
                    )
                    if center_copy_status and not center_copy_sent and "failed" in center_copy_status.lower():
                        flash(center_copy_status, "warning")
                    email_sent = True
                else:
                    flash(
                        f"Failed to send confirmation email to {guardian_email}: {email_result.get('error', 'unknown error')}",
                        "warning",
                    )
            else:
                flash(
                    f"Cannot send email: No valid email address found for guardian.",
                    "warning",
                )

        return render_template(
            "cancellation_notice.html",
            options=options,
            selected_option=selected_option,
            selected_students=selected_students,
            notice_date=notice_date.isoformat(),
            notice_date_display=_format_long_date(notice_date),
            effective_last_attendance_date=effective_date.isoformat(),
            effective_last_attendance_date_display=_format_long_date(effective_date),
            last_payment_date=last_payment_date.isoformat() if last_payment_date else None,
            last_payment_date_display=_format_long_date(last_payment_date) if last_payment_date else None,
            requires_payment=requires_payment,
            email_sent=email_sent,
            center_name=center_name,
            center_phone=center_phone,
            center_phone_display=center_phone or "Not set",
            center_email=center_email,
            center_location=center_location,
            center_address=center_address,
        )

    try:
        from app import csrf

        csrf.exempt(cancellation_notice_page)
    except Exception:
        # If CSRF isn't available during a nonstandard import path, keep the route registered.
        pass
