# routes/schedule.py
import io

from flask import render_template, request, jsonify, send_file, url_for
from datetime import datetime, timedelta
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfgen import canvas
from werkzeug.routing import BuildError
from modules import schedule_manager, instructor_profile_manager, assistant_manager, auth_manager
from routes.auth import require_login, require_feature


def register_schedule_routes(app):
    """Register assistant scheduling routes."""

    def _read_app_version():
        """Read version from VERSION file."""
        try:
            with open("VERSION", "r") as f:
                return f.read().strip()
        except Exception:
            return "unknown"

    # Same palette as the UI (schedule_assistants.html JS)
    _ASSISTANT_PALETTE = [
        HexColor('#2E7D32'),  # green
        HexColor('#1565C0'),  # blue
        HexColor('#6A1B9A'),  # purple
        HexColor('#EF6C00'),  # orange
        HexColor('#00897B'),  # teal
        HexColor('#C62828'),  # red
        HexColor('#5D4037'),  # brown
        HexColor('#283593'),  # indigo
        HexColor('#AD1457'),  # pink
        HexColor('#37474F'),  # blue-grey
    ]

    def _assistant_color(assistant_id):
        idx = abs(int(assistant_id or 0)) % len(_ASSISTANT_PALETTE)
        return _ASSISTANT_PALETTE[idx]

    def _get_operating_weekdays(profile):
        """Return a set of weekday indices (0=Mon..6=Sun) when center is operating."""
        days_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        open_days = set()
        if not profile:
            return open_days
        for day_name, day_idx in days_map.items():
            if profile.get(f"{day_name}_start"):
                open_days.add(day_idx)
        return open_days

    @app.route("/schedule/assistants")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def schedule_assistants():
        """Display calendar-based assistant scheduling interface."""
        # Get current month/year (or from query params)
        today = datetime.today().date()
        year = request.args.get("year", default=today.year, type=int)
        month = request.args.get("month", default=today.month, type=int)
        
        # Validate month bounds
        if month < 1:
            month = 12
            year -= 1
        elif month > 12:
            month = 1
            year += 1
        
        # Get instructor profile to determine operating hours
        profile = instructor_profile_manager.get_instructor_profile()
        
        # Map center operating hours by day of week (0=Monday, 6=Sunday)
        operating_days = {}
        if profile:
            days_map = {
                "monday": 0,
                "tuesday": 1,
                "wednesday": 2,
                "thursday": 3,
                "friday": 4,
                "saturday": 5,
                "sunday": 6,
            }
            for day_name, day_idx in days_map.items():
                start_col = f"{day_name}_start"
                end_col = f"{day_name}_end"
                start_time = profile.get(start_col)
                end_time = profile.get(end_col)
                if start_time:
                    operating_days[day_idx] = {
                        "start": start_time,
                        "end": end_time,
                    }
        
        # Build calendar grid
        first_day = datetime(year, month, 1).date()
        first_weekday = first_day.weekday()  # 0=Monday
        
        # Calculate number of days in month
        if month == 12:
            last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
        else:
            last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)
        
        days_in_month = last_day.day
        
        # Get scheduled assistants for this month
        scheduled = schedule_manager.get_assistants_schedule_for_month(year, month)
        closed_dates = schedule_manager.get_center_closed_dates_for_month(year, month)
        
        # Get all unscheduled assistants (pool)
        all_assistants = assistant_manager.get_all_assistants()
        
        # Build calendar weeks
        calendar_weeks = []
        week = [None] * 7  # Monday to Sunday
        for offset in range(first_weekday):
            week[offset] = None  # Empty cells before month starts
        
        for day_num in range(1, days_in_month + 1):
            date_obj = datetime(year, month, day_num).date()
            date_str = date_obj.isoformat()
            weekday = date_obj.weekday()
            
            weekday_is_operating = weekday in operating_days
            is_center_closed = date_str in closed_dates
            is_operating = weekday_is_operating and not is_center_closed
            scheduled_assistants = scheduled.get(date_str, [])
            
            week[weekday] = {
                "day": day_num,
                "date": date_str,
                "weekday_is_operating": weekday_is_operating,
                "is_center_closed": is_center_closed,
                "is_operating": is_operating,
                "assistants": scheduled_assistants,
            }
            
            # End of week or end of month
            if weekday == 6:
                calendar_weeks.append(week)
                week = [None] * 7
        
        # Add final week if it has any days
        if any(w is not None for w in week):
            calendar_weeks.append(week)
        
        try:
            pdf_url = url_for("schedule_assistants_pdf", year=year, month=month)
        except BuildError:
            # Fallback for stale server/module states; avoids template hard failure.
            pdf_url = f"/schedule/assistants/pdf?year={year}&month={month}"

        return render_template(
            "schedule_assistants.html",
            year=year,
            month=month,
            month_name=datetime(year, month, 1).strftime("%B"),
            calendar_weeks=calendar_weeks,
            operating_days=operating_days,
            all_assistants=all_assistants,
            today=today.isoformat(),
            pdf_url=pdf_url,
        )

    @app.route("/api/schedule/assign", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_schedule_assign():
        """Assign an assistant to a date (drag-drop or form submission)."""
        data = request.get_json(silent=True) or {}

        try:
            assistant_id = int(data.get("assistant_id")) if data.get("assistant_id") is not None else None
        except (TypeError, ValueError):
            assistant_id = None
        scheduled_date = data.get("scheduled_date")
        
        if not assistant_id or not scheduled_date:
            return jsonify({"error": "Missing assistant_id or scheduled_date"}), 400

        # Validate date format and normalize
        try:
            parsed_date = datetime.strptime(str(scheduled_date), "%Y-%m-%d").date()
            scheduled_date = parsed_date.isoformat()
        except ValueError:
            return jsonify({"error": "Invalid scheduled_date format; expected YYYY-MM-DD"}), 400
        
        # Verify assistant belongs to this user
        asst = assistant_manager.get_assistant(assistant_id)
        if not asst:
            return jsonify({"error": "Staff member not found"}), 404

        # Enforce business rule server-side: assignments only on operating days
        profile = instructor_profile_manager.get_instructor_profile()
        operating_weekdays = _get_operating_weekdays(profile)
        if parsed_date.weekday() not in operating_weekdays:
            return jsonify({"error": "Cannot schedule staff on closed days"}), 400
        if schedule_manager.is_center_closed_date(scheduled_date):
            return jsonify({"error": "Cannot schedule staff on center-closed dates"}), 400
        
        success = schedule_manager.schedule_assistant(
            assistant_id, scheduled_date)
        
        if success:
            return jsonify({"success": True, "message": "Staff scheduled"}), 200

        # Distinguish duplicate from other DB write errors when possible
        if schedule_manager.is_assistant_scheduled(assistant_id, scheduled_date):
            return jsonify({"error": "Staff member already scheduled for this date"}), 400
        return jsonify({"error": "Unable to schedule staff member"}), 400

    @app.route("/api/schedule/unassign", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_schedule_unassign():
        """Remove an assistant from a scheduled date."""
        data = request.get_json(silent=True) or {}

        try:
            assistant_id = int(data.get("assistant_id")) if data.get("assistant_id") is not None else None
        except (TypeError, ValueError):
            assistant_id = None
        scheduled_date = data.get("scheduled_date")
        
        if not assistant_id or not scheduled_date:
            return jsonify({"error": "Missing assistant_id or scheduled_date"}), 400

        # Validate date format and normalize
        try:
            scheduled_date = datetime.strptime(str(scheduled_date), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return jsonify({"error": "Invalid scheduled_date format; expected YYYY-MM-DD"}), 400
        
        count = schedule_manager.unschedule_assistant(
            assistant_id, scheduled_date)
        
        if count > 0:
            return jsonify({"success": True, "message": "Staff unscheduled"}), 200
        else:
            return jsonify({"error": "Staff member not found in schedule"}), 400

    @app.route("/api/schedule/mark-closed", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_schedule_mark_closed():
        """Mark a specific date as center closed (holiday/closure)."""
        data = request.get_json(silent=True) or {}

        scheduled_date = data.get("scheduled_date")
        reason = data.get("reason") or "Holiday / Center Closed"

        if not scheduled_date:
            return jsonify({"error": "Missing scheduled_date"}), 400

        try:
            parsed_date = datetime.strptime(str(scheduled_date), "%Y-%m-%d").date()
            scheduled_date = parsed_date.isoformat()
        except ValueError:
            return jsonify({"error": "Invalid scheduled_date format; expected YYYY-MM-DD"}), 400

        # Only allow explicit closure override on otherwise operating weekdays.
        profile = instructor_profile_manager.get_instructor_profile()
        operating_weekdays = _get_operating_weekdays(profile)
        if parsed_date.weekday() not in operating_weekdays:
            return jsonify({"error": "Date is already closed by center weekly operating hours"}), 400

        inserted = schedule_manager.set_center_closed_date(scheduled_date, reason)
        removed_count = schedule_manager.unschedule_all_assistants_for_date(scheduled_date)

        return jsonify({
            "success": True,
            "message": "Center marked closed",
            "already_closed": not inserted,
            "removed_assistants": removed_count,
        }), 200

    @app.route("/api/schedule/unmark-closed", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def api_schedule_unmark_closed():
        """Remove center closed override for a specific date."""
        data = request.get_json(silent=True) or {}

        scheduled_date = data.get("scheduled_date")
        if not scheduled_date:
            return jsonify({"error": "Missing scheduled_date"}), 400

        try:
            parsed_date = datetime.strptime(str(scheduled_date), "%Y-%m-%d").date()
            scheduled_date = parsed_date.isoformat()
        except ValueError:
            return jsonify({"error": "Invalid scheduled_date format; expected YYYY-MM-DD"}), 400

        profile = instructor_profile_manager.get_instructor_profile()
        operating_weekdays = _get_operating_weekdays(profile)
        if parsed_date.weekday() not in operating_weekdays:
            return jsonify({"error": "Cannot reopen non-operating weekday"}), 400

        removed = schedule_manager.unset_center_closed_date(scheduled_date)
        return jsonify({
            "success": True,
            "message": "Center reopened",
            "was_closed": removed > 0,
        }), 200

    @app.route("/schedule/assistants/pdf")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def schedule_assistants_pdf():
        """Export monthly assistant schedule as PDF."""
        today = datetime.today().date()
        year = request.args.get("year", default=today.year, type=int)
        month = request.args.get("month", default=today.month, type=int)

        if month < 1:
            month = 12
            year -= 1
        elif month > 12:
            month = 1
            year += 1

        profile = instructor_profile_manager.get_instructor_profile()
        operating_days = _get_operating_weekdays(profile)

        first_day = datetime(year, month, 1).date()
        first_weekday = first_day.weekday()
        if month == 12:
            last_day = datetime(year + 1, 1, 1).date() - timedelta(days=1)
        else:
            last_day = datetime(year, month + 1, 1).date() - timedelta(days=1)

        scheduled = schedule_manager.get_assistants_schedule_for_month(year, month)
        closed_dates = schedule_manager.get_center_closed_dates_for_month(year, month)

        weeks = []
        week = [None] * 7
        for offset in range(first_weekday):
            week[offset] = None

        for day_num in range(1, last_day.day + 1):
            date_obj = datetime(year, month, day_num).date()
            date_str = date_obj.isoformat()
            weekday = date_obj.weekday()
            weekday_is_operating = weekday in operating_days
            is_center_closed = date_str in closed_dates
            week[weekday] = {
                "day": day_num,
                "date": date_str,
                "weekday_is_operating": weekday_is_operating,
                "is_center_closed": is_center_closed,
                "is_operating": weekday_is_operating and not is_center_closed,
                "assistants": scheduled.get(date_str, []),
            }
            if weekday == 6:
                weeks.append(week)
                week = [None] * 7
        if any(w is not None for w in week):
            weeks.append(week)

        buffer = io.BytesIO()
        canv = canvas.Canvas(buffer, pagesize=landscape(letter))
        width, height = landscape(letter)

        month_title = datetime(year, month, 1).strftime("%B %Y")
        app_version = _read_app_version()
        canv.setFont("Helvetica-Bold", 15)
        canv.drawString(0.6 * inch, height - 0.6 * inch, f"Staff Schedule — {month_title}")
        canv.setFont("Helvetica", 8)
        canv.drawRightString(width - 0.6 * inch, height - 0.6 * inch, f"v{app_version}")
        canv.setFont("Helvetica", 9)
        canv.drawString(0.6 * inch, height - 0.85 * inch, "Open days are labeled 'Open'.")

        left = 0.45 * inch
        right = width - 0.45 * inch
        top = height - 1.1 * inch
        bottom = 0.45 * inch

        grid_w = right - left
        grid_h = top - bottom
        cols = 7
        rows = max(len(weeks) + 1, 2)  # +1 header row
        cell_w = grid_w / cols
        cell_h = grid_h / rows

        day_headers = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        canv.setFont("Helvetica-Bold", 9)
        for c in range(cols):
            x = left + c * cell_w
            y = top - cell_h
            canv.rect(x, y, cell_w, cell_h, stroke=1, fill=0)
            canv.drawString(x + 4, y + cell_h - 12, day_headers[c])

        for row_idx, week_data in enumerate(weeks, start=1):
            y = top - (row_idx + 1) * cell_h
            for col_idx in range(cols):
                x = left + col_idx * cell_w
                day = week_data[col_idx]
                canv.setFillColor(black)
                canv.setStrokeColor(black)
                canv.rect(x, y, cell_w, cell_h, stroke=1, fill=0)

                if not day:
                    continue

                canv.setFillColor(black)
                canv.setFont("Helvetica-Bold", 9)
                canv.drawString(x + 4, y + cell_h - 12, str(day["day"]))

                canv.setFont("Helvetica", 7)
                if day.get("is_center_closed"):
                    status = "Holiday"
                else:
                    status = "Open" if day["is_operating"] else "Closed"
                canv.drawRightString(x + cell_w - 4, y + cell_h - 12, status)

                assistants = day["assistants"]
                badge_h = 9
                badge_gap = 2
                badge_step = badge_h + badge_gap
                badge_w = cell_w - 8
                max_badges = max(int((cell_h - 20) // badge_step), 1)
                overflow = max(0, len(assistants) - max_badges)
                visible = assistants if not overflow else assistants[:max_badges - 1]

                ty = y + cell_h - 22
                for asst in visible:
                    if ty < y + 3:
                        break
                    asst_id = asst[0]
                    asst_name = str(asst[1])
                    badge_color = _assistant_color(asst_id)
                    # Draw colored background rectangle
                    canv.setFillColor(badge_color)
                    canv.setStrokeColor(badge_color)
                    canv.rect(x + 4, ty - 1, badge_w, badge_h, stroke=0, fill=1)
                    # Draw white label text on top
                    canv.setFillColor(white)
                    canv.setFont("Helvetica", 6.5)
                    max_chars = max(int(badge_w / 5.5), 4)
                    canv.drawString(x + 6, ty, asst_name[:max_chars])
                    ty -= badge_step

                canv.setFillColor(black)
                canv.setStrokeColor(black)
                if overflow:
                    canv.setFont("Helvetica", 6)
                    canv.drawString(x + 4, ty, f"+{overflow} more")

        canv.save()
        buffer.seek(0)
        filename = f"assistant_schedule_{year:04d}_{month:02d}_v{app_version}.pdf"
        return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)
