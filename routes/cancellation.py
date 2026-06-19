import calendar
import json
import sqlite3
from datetime import date, datetime

from flask import flash, redirect, render_template, request, url_for

from modules import instructor_profile_manager, student_manager
from modules.database import DB_PATH
from modules.email_manager import get_email_manager, render_branded_email_shell, resolve_center_name
from routes.auth import require_login


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


def _group_identity(student_item: dict) -> tuple[str, str, str] | None:
    guardian = str(student_item.get("guardian") or "").strip().lower()
    email = str(student_item.get("email") or "").strip().lower()
    phone = str(student_item.get("phone") or "").strip().lower()
    if not any((guardian, email, phone)):
        return None
    return guardian, email, phone


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

    family_groups = {}
    for student in student_rows:
        key = _group_identity(student)
        if not key:
            continue
        family_groups.setdefault(key, []).append(student)

    family_idx = 1
    for key, members in family_groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda x: x["name"].lower())
        guardian, email, phone = key
        family_label_base = guardian or email or phone or "Family"
        token = f"family:{family_idx}"
        family_idx += 1
        label = f"Family/Siblings: {family_label_base} ({len(members)} students)"
        options.append({"value": token, "label": label})
        option_map[token] = [m["id"] for m in members]

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


def _send_center_ack_email(
    center_email: str,
    customer_name: str,
    customer_email: str,
    selected_students: list[dict],
    notice_date: date,
    effective_date: date,
):
    email_manager = get_email_manager()
    center_name = resolve_center_name()

    student_names = [s.get("name", "") for s in selected_students if s.get("name")]
    students_display = ", ".join(student_names) if student_names else "N/A"

    subject = f"{center_name} — Cancellation Notice Acknowledgment"
    plain_body = (
        f"Cancellation Notice Acknowledgment\n\n"
        f"Customer Name: {customer_name}\n"
        f"Customer Email: {customer_email or 'N/A'}\n"
        f"Student(s): {students_display}\n"
        f"Notice Date: {notice_date.isoformat()}\n"
        f"Effective Last Attendance Date: {effective_date.isoformat()}\n"
        f"Acknowledgment Method: Checkbox + Typed Name\n"
        f"Acknowledged At: {datetime.now().isoformat(timespec='seconds')}\n"
    )

    html_body = render_branded_email_shell(
        title="Cancellation Notice Acknowledgment",
        center_name=center_name,
        subtitle=center_name,
        footer_note=f"This is an automated message from {center_name}. Please do not reply to this email.",
        body_html=(
            "<p>A customer acknowledged a cancellation notice.</p>"
            "<table class='report-table'>"
            f"<tr><th>Customer Name</th><td>{customer_name}</td></tr>"
            f"<tr><th>Customer Email</th><td>{customer_email or 'N/A'}</td></tr>"
            f"<tr><th>Student(s)</th><td>{students_display}</td></tr>"
            f"<tr><th>Notice Date</th><td>{notice_date.isoformat()}</td></tr>"
            f"<tr><th>Effective Last Attendance Date</th><td>{effective_date.isoformat()}</td></tr>"
            "<tr><th>Acknowledgment Method</th><td>Checkbox + Typed Name</td></tr>"
            f"<tr><th>Acknowledged At</th><td>{datetime.now().isoformat(timespec='seconds')}</td></tr>"
            "</table>"
        ),
    )

    return email_manager.send_email(
        recipient_email=center_email,
        subject=subject,
        body=plain_body,
        html_body=html_body,
        no_reply=True,
    )


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
                flash("Please select a student or family group.", "warning")
                return redirect(url_for("cancellation_notice_page"))

            selected_students = _selected_students(student_rows, selected_ids)
            if not selected_students:
                flash("Selected students could not be loaded.", "warning")
                return redirect(url_for("cancellation_notice_page"))

            notice_date = _parse_notice_date(request.form.get("notice_date", ""))
            effective_date = _last_day_of_next_month(notice_date)

            customer_name = str(request.form.get("customer_name") or "").strip()
            customer_email = str(request.form.get("customer_email") or "").strip()
            identity_confirmed = bool(request.form.get("identity_confirmed"))
            last_day_confirmed = bool(request.form.get("last_day_confirmed"))

            if not customer_name:
                flash("Please enter typed customer name.", "warning")
                return redirect(url_for("cancellation_notice_page", selection=selected_option))
            if not identity_confirmed or not last_day_confirmed:
                flash("Both acknowledgement checkboxes are required.", "warning")
                return redirect(url_for("cancellation_notice_page", selection=selected_option))

            profile = instructor_profile_manager.get_instructor_profile() or {}
            center_email = str(profile.get("email") or "").strip()
            email_result = {"success": False, "error": "Center email is not configured in Center Profile."}
            if center_email and "@" in center_email:
                email_result = _send_center_ack_email(
                    center_email=center_email,
                    customer_name=customer_name,
                    customer_email=customer_email,
                    selected_students=selected_students,
                    notice_date=notice_date,
                    effective_date=effective_date,
                )

            student_names = [s["name"] for s in selected_students]
            selection_label = next((o["label"] for o in options if o["value"] == selected_option), "")
            notice_id = _insert_cancellation_notice(
                student_ids=selected_ids,
                student_names=student_names,
                selection_label=selection_label,
                notice_date=notice_date,
                effective_last_attendance_date=effective_date,
                customer_name=customer_name,
                customer_email=customer_email,
                center_email_sent=bool(email_result.get("success")),
                center_email_status=email_result.get("message") or email_result.get("error") or "",
            )

            if email_result.get("success"):
                flash(f"Cancellation notice acknowledged and emailed to center (Notice #{notice_id}).", "success")
            else:
                flash(
                    f"Cancellation notice acknowledged (Notice #{notice_id}), but center email failed: {email_result.get('error', 'unknown error')}",
                    "warning",
                )

            return redirect(url_for("cancellation_notice_page", selection=selected_option))

        selected_option = str(request.args.get("selection") or "").strip()
        if selected_option not in option_map and options:
            selected_option = options[0]["value"]

        notice_date = _parse_notice_date(request.args.get("notice_date", ""))
        effective_date = _last_day_of_next_month(notice_date)

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
