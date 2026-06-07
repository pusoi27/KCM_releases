import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import date, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Keep this probe non-invasive:
# - bypass license gate for local probe
# - skip startup version auto-bump path in app import
os.environ.setdefault("DEV_LICENSE_BYPASS", "true")
os.environ.setdefault("FLASK_USE_RELOADER", "true")

import app as app_module  # noqa: E402
from modules import instructor_profile_manager, user_identity_manager  # noqa: E402


def _next_operating_weekday() -> str:
    """Return next weekday date string (Mon-Fri) for schedule CRUD probe."""
    d = date.today() + timedelta(days=1)
    while d.weekday() >= 5:  # skip Sat/Sun
        d += timedelta(days=1)
    return d.isoformat()


def _ensure_profile_and_identity() -> None:
    email = "dev@localhost.local"
    user_identity_manager.save_email(email)

    profile = instructor_profile_manager.get_instructor_profile()
    weekly = {
        "monday_start": "09:00", "monday_end": "17:00",
        "tuesday_start": "09:00", "tuesday_end": "17:00",
        "wednesday_start": "09:00", "wednesday_end": "17:00",
        "thursday_start": "09:00", "thursday_end": "17:00",
        "friday_start": "09:00", "friday_end": "17:00",
        "saturday_start": "", "saturday_end": "",
        "sunday_start": "", "sunday_end": "",
    }

    if profile and profile.get("id"):
        instructor_profile_manager.update_instructor_profile(
            profile_id=profile["id"],
            name=profile.get("name") or "Runtime Probe",
            email=profile.get("email") or email,
            phone=profile.get("phone") or "",
            center_location=profile.get("center_location") or "",
            center_address=profile.get("center_address") or "",
            center_time_zone=profile.get("center_time_zone") or "",
            center_hours=profile.get("center_hours") or "",
            weekly_hours=weekly,
        )
    else:
        instructor_profile_manager.create_instructor_profile(
            name="Runtime Probe",
            email=email,
            phone="",
            center_location="",
            center_address="",
            center_time_zone="",
            center_hours="",
            weekly_hours=weekly,
        )


def _capture_request_logs(client, method: str, path: str, json_payload: dict):
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        if method.upper() == "POST":
            resp = client.post(path, json=json_payload)
        elif method.upper() == "DELETE":
            resp = client.delete(path, json=json_payload)
        else:
            resp = client.get(path, query_string=json_payload)
    return resp, buf.getvalue()


def main() -> int:
    _ensure_profile_and_identity()

    app = app_module.app
    app.config["WTF_CSRF_ENABLED"] = False

    target_date = _next_operating_weekday()
    client = app.test_client()

    resp_mark, logs_mark = _capture_request_logs(
        client,
        "POST",
        "/api/schedule/mark-closed",
        {"scheduled_date": target_date, "reason": "Runtime probe"},
    )

    resp_unmark, logs_unmark = _capture_request_logs(
        client,
        "POST",
        "/api/schedule/unmark-closed",
        {"scheduled_date": target_date},
    )

    combined_logs = "\n".join([logs_mark, logs_unmark])

    forbidden_markers = [
        "Immediate post-write backup push failed",
        "[sync] Pushed DB to",
        "[sync] Snapshot ->",
        "Final exit push",
        "post-write backup push",
    ]

    found = [m for m in forbidden_markers if m in combined_logs]

    print("=== Runtime auto-push probe ===")
    print(f"Request 1: POST /api/schedule/mark-closed -> {resp_mark.status_code}")
    print(f"Request 2: POST /api/schedule/unmark-closed -> {resp_unmark.status_code}")
    print("--- Captured request-time logs ---")
    trimmed = combined_logs.strip()
    print(trimmed if trimmed else "(no request-time log output captured)")
    print("--- Verdict ---")
    if found:
        print("FAIL: Detected backup auto-push related log markers:")
        for marker in found:
            print(f" - {marker}")
        return 1

    print("PASS: No post-write backup auto-push log markers detected during CRUD POST requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
