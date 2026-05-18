import json
import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from modules import instructor_profile_manager, student_manager
from modules.database import DB_PATH
from modules.email_manager import render_branded_email_shell, resolve_center_name
from modules.utils import duration_seconds


def fmt(value: str) -> str:
    if not value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %I:%M:%S %p")
    except Exception:
        return str(value)


conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

row = None
for variant in ("ace s.", "ace s"):
    row = c.execute(
        "SELECT id, name FROM students WHERE lower(trim(name)) = ? AND active = 1 LIMIT 1",
        (variant,),
    ).fetchone()
    if row:
        break

if not row:
    row = c.execute(
        "SELECT id, name FROM students WHERE lower(name) LIKE ? AND active = 1 ORDER BY id LIMIT 1",
        ("%ace s%",),
    ).fetchone()

if not row:
    raise SystemExit("Student Ace S. not found in active students table")

student_id, student_name = row
student = student_manager.get_student(student_id)
if not student:
    raise SystemExit("Student record not resolvable via student_manager.get_student")

open_row = c.execute(
    "SELECT id, start_time FROM sessions WHERE student_id = ? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
    (student_id,),
).fetchone()

scenario = "active_checkout"
if open_row:
    _, start_time = open_row
    end_time = datetime.now().isoformat()
else:
    latest = c.execute(
        "SELECT start_time, end_time FROM sessions WHERE student_id = ? AND end_time IS NOT NULL ORDER BY id DESC LIMIT 1",
        (student_id,),
    ).fetchone()
    if latest:
        start_time, end_time = latest
        scenario = "latest_completed_session_fallback"
    else:
        end_time = datetime.now().isoformat()
        start_time = end_time
        scenario = "no_session_fallback"

conn.close()

try:
    total_seconds = max(0, int(duration_seconds(start_time, end_time)))
except Exception:
    total_seconds = 0

hours = total_seconds // 3600
minutes = (total_seconds % 3600) // 60
seconds = total_seconds % 60
duration_display = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

profile = instructor_profile_manager.get_instructor_profile() or {}
center_name = str(profile.get("center_location") or "").strip() or resolve_center_name()

guardian_name = str(student[22] or "").strip() if len(student) > 22 else ""
salutation = f"Dear {guardian_name}," if guardian_name else "Dear Parent/Guardian,"

email_subject = f"{center_name} - Class Checkout - {student_name}"

html_body = render_branded_email_shell(
    title=f"{center_name} Class Checkout Confirmation",
    center_name=center_name,
    subtitle=center_name,
    footer_note=f"This is an automated checkout message from {center_name}. Please do not reply to this email.",
    body_html=(
        f"<p>{salutation}</p>"
        f"<div class=\"highlight\"><strong>{student_name}</strong> has checked out from class.</div>"
        f"<table class=\"report-table\">"
        f"<tr><th>Guardian</th><td>{guardian_name or 'Parent/Guardian'}</td></tr>"
        f"<tr><th>Start Time</th><td>{fmt(start_time)}</td></tr>"
        f"<tr><th>End Time</th><td>{fmt(end_time)}</td></tr>"
        f"<tr><th>Session Duration</th><td>{duration_display}</td></tr>"
        f"<tr><th>Center</th><td>{center_name}</td></tr>"
        f"</table>"
    ),
)

os.makedirs("exports", exist_ok=True)
preview_file = os.path.abspath(os.path.join("exports", "ace_s_checkout_email_preview.html"))
with open(preview_file, "w", encoding="utf-8") as fh:
    fh.write(html_body)

meta = {
    "student_id": student_id,
    "student_name": student_name,
    "recipient_email": (student[3] if len(student) > 3 else ""),
    "subject": email_subject,
    "scenario": scenario,
    "start_time": start_time,
    "end_time": end_time,
    "duration": duration_display,
    "center_name": center_name,
    "preview_file": preview_file,
}

print(json.dumps(meta, indent=2))
