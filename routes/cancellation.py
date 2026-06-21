import calendar
import io
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

from flask import flash, redirect, render_template, request, url_for

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

    Policy: notice date + 30 days, capped at last day of next month.
    Example: Jan 31 + 30 days would spill into March, so cap to Feb 28/29.
    """
    candidate = notice_date + timedelta(days=30)
    next_month_last = _last_day_of_next_month(notice_date)
    return min(candidate, next_month_last)


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
    c.drawString(left, y, "Acknowledgement")
    y -= 0.24 * inch
    c.setFont("Helvetica", 10)
    text = (
        "I acknowledge this cancellation notice and understand the effective last attendance date shown above. "
        "Please digitally sign this PDF and forward the signed copy to the center email listed above for record keeping."
    )
    text_obj = c.beginText(left, y)
    text_obj.setFont("Helvetica", 10)
    text_obj.setLeading(14)
    for line in [text[i:i + 105] for i in range(0, len(text), 105)]:
        text_obj.textLine(line)
    c.drawText(text_obj)

    y -= 1.05 * inch
    c.setFont("Helvetica", 11)
    c.drawString(left, y, "Customer Digital Signature: _________________________________")
    y -= 0.32 * inch
    c.drawString(left, y, "Customer Name (typed): ______________________________________")
    y -= 0.32 * inch
    c.drawString(left, y, "Signature Date: __________________")

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
        f"Important: Please digitally sign the attached PDF and then forward this email (with the signed PDF) to {center_email or 'the center email listed in the PDF'} for record keeping.\n\n"
        f"This message is sent from no-reply flow in Stdytime; replies are not monitored.\n"
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
            f"<div class='highlight'>After digitally signing the PDF, forward this email (with the signed PDF) to the center email for record keeping.</div>"
        ),
    )

    return email_manager.send_email(
        recipient_email=customer_email,
        subject=subject,
        body=plain_body,
        html_body=html_body,
        attachments=[pdf_path],
        no_reply=True,
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

        if request.method == "POST":
            selected_option = str(request.form.get("selection") or "").strip()
            selected_ids = option_map.get(selected_option, [])
            if not selected_ids:
                flash("Please select a student.", "warning")
                return redirect(url_for("cancellation_notice_page"))

            selected_students = _selected_students(student_rows, selected_ids)
            if not selected_students:
                flash("Selected students could not be loaded.", "warning")
                return redirect(url_for("cancellation_notice_page"))
            student_item = selected_students[0]

            notice_date = date.today()
            effective_date = _effective_last_attendance_date(notice_date)

            customer_email = str(request.form.get("customer_email") or "").strip()
            if not customer_email or "@" not in customer_email:
                flash("A valid customer email is required.", "warning")
                return redirect(url_for("cancellation_notice_page", selection=selected_option))

            profile = instructor_profile_manager.get_instructor_profile() or {}
            center_email = str(profile.get("email") or "").strip()
            center_name = resolve_center_name()
            pdf_path = _build_cancellation_notice_pdf(
                student_item=student_item,
                notice_date=notice_date,
                effective_date=effective_date,
                center_name=center_name,
                center_email=center_email,
            )

            email_result = _send_customer_cancellation_notice_email(
                center_email=center_email,
                center_name=center_name,
                student_item=student_item,
                notice_date=notice_date,
                effective_date=effective_date,
                pdf_path=pdf_path,
                customer_email=customer_email,
            )

            selection_label = next((o["label"] for o in options if o["value"] == selected_option), "")
            notice_id = _insert_cancellation_notice_email_dispatch(
                student_item=student_item,
                selection_label=selection_label,
                notice_date=notice_date,
                effective_last_attendance_date=effective_date,
                customer_email=customer_email,
                center_email_sent=bool(email_result.get("success")),
                center_email_status=email_result.get("message") or email_result.get("error") or "",
            )

            if email_result.get("success"):
                sender_email = str(getattr(get_email_manager(), "sender_email", "") or "").strip()
                sender_note = ""
                if sender_email and sender_email.lower() != "noreply@stdytime.com":
                    sender_note = f" Sent via configured sender {sender_email} (no-reply flow enabled)."
                flash(
                    f"Cancellation notice sent to customer with prefilled PDF (Notice #{notice_id}). "
                    f"Customer must digitally sign and forward it to {center_email or 'the center email shown in the PDF'}."
                    f"{sender_note}",
                    "success",
                )
            else:
                flash(
                    f"Cancellation notice PDF generated (Notice #{notice_id}), but customer email failed: {email_result.get('error', 'unknown error')}",
                    "warning",
                )

            return redirect(url_for("cancellation_notice_page", selection=selected_option))

        selected_option = str(request.args.get("selection") or "").strip()
        if selected_option not in option_map and options:
            selected_option = options[0]["value"]

        notice_date = date.today()
        effective_date = _effective_last_attendance_date(notice_date)

        selected_ids = option_map.get(selected_option, [])
        selected_students = _selected_students(student_rows, selected_ids) if selected_ids else []

        default_customer_email = ""
        if selected_students:
            first = selected_students[0]
            default_customer_email = str(first.get("email") or "").strip()

        return render_template(
            "cancellation_notice.html",
            options=options,
            selected_option=selected_option,
            selected_students=selected_students,
            notice_date=notice_date.isoformat(),
            effective_last_attendance_date=effective_date.isoformat(),
            default_customer_email=default_customer_email,
        )
