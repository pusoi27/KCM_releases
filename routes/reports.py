# routes/reports.py
import io
import csv
import sqlite3
from datetime import datetime, timedelta
from flask import render_template, request, send_file, redirect, url_for
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from modules import reports, student_manager, book_manager, auth_manager
from modules.database import DB_PATH
from routes.auth import require_login, require_feature

def register_reports_routes(app):
    """Register reports routes (staff hours and attendance)."""
    
    # ================================================================
    # Staff Hours Reports
    # ================================================================
    
    @app.route('/reports/assistants')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def reports_assistants():
        """Display staff hours report page with date range selection."""
        # Default date suggestions (last 30 days)
        today = datetime.today().date()
        default_start = (today - timedelta(days=30)).isoformat()
        default_end = today.isoformat()
        
        # Get parameters from query string
        start_param = request.args.get('start', '')
        end_param = request.args.get('end', '')
        
        # Use provided params or defaults for display
        display_start = start_param if start_param else default_start
        display_end = end_param if end_param else default_end
        
        error = None
        staff_hours = []
        show_data = False
        
        # Only fetch data if both dates are provided
        if start_param and end_param:
            show_data = True
            # Validate inputs
            try:
                start_date = datetime.strptime(start_param, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_param, '%Y-%m-%d').date()
            except ValueError:
                error = "Invalid date format; expected YYYY-MM-DD"
                show_data = False
                start_date = None
                end_date = None
            
            if show_data and start_date and end_date:
                if end_date < start_date:
                    error = "End date must be on or after start date"
                    show_data = False
            
            # Fetch data if no error
            if show_data and not error and start_date and end_date:
                summary = reports.get_assistant_hours_between(start_date.isoformat(), end_date.isoformat())
                # Convert to HH:MM format
                for name, sessions_count, total_sec in summary:
                    hours = int(total_sec // 3600)
                    minutes = int((total_sec % 3600) // 60)
                    time_str = f"{hours:02d}:{minutes:02d}"
                    staff_hours.append({
                        'name': name,
                        'sessions': sessions_count,
                        'total_time': time_str,
                        'total_seconds': total_sec
                    })
        
        return render_template(
            'reports_assistants.html',
            start=display_start,
            end=display_end,
            error=error,
            staff_hours=staff_hours,
            show_data=show_data
        )

    @app.route('/reports/assistants/pdf')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def reports_assistants_pdf():
        """Generate PDF of staff hours report with date range."""
        start_param = request.args.get('start')
        end_param = request.args.get('end')
        
        if not start_param or not end_param:
            # Fallback to last 30 days
            today = datetime.today().date()
            start_param = (today - timedelta(days=30)).isoformat()
            end_param = today.isoformat()
        
        try:
            start_date = datetime.strptime(start_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_param, '%Y-%m-%d').date()
        except ValueError:
            return "Invalid date format; expected YYYY-MM-DD", 400
        
        if end_date < start_date:
            return "End date must be on or after start date", 400
        
        summary = reports.get_assistant_hours_between(start_param, end_param)
        
        buffer = io.BytesIO()
        canv = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter
        
        canv.setFont("Helvetica-Bold", 16)
        canv.drawString(inch, height - inch, "Payroll Staff Hours Report")
        canv.setFont("Helvetica", 12)
        canv.drawString(inch, height - inch - 0.3*inch, f"Date Range: {start_date} to {end_date}")
        
        y = height - inch - 0.7*inch
        canv.setFont("Helvetica-Bold", 11)
        canv.drawString(inch, y, "Staff Name")
        canv.drawString(inch + 3*inch, y, "Sessions")
        canv.drawString(inch + 4.5*inch, y, "Total Hours")
        
        y -= 0.3*inch
        canv.setFont("Helvetica", 10)
        
        for name, sessions_count, total_sec in summary:
            hours = int(total_sec // 3600)
            minutes = int((total_sec % 3600) // 60)
            time_str = f"{hours:02d}:{minutes:02d}"
            
            canv.drawString(inch, y, str(name))
            canv.drawString(inch + 3*inch, y, str(sessions_count))
            canv.drawString(inch + 4.5*inch, y, time_str)
            y -= 0.25*inch
            
            if y < inch:
                canv.showPage()
                canv.setFont("Helvetica", 10)
                y = height - inch
        
        canv.save()
        buffer.seek(0)
        filename = f"payroll_staff_hours_{start_date}_to_{end_date}.pdf"
        return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)

    @app.route('/reports/assistants/csv')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def reports_assistants_csv():
        """Generate CSV of staff duty log with date range."""
        start = request.args.get('start')
        end = request.args.get('end')
        if not start or not end:
            return redirect(url_for('reports_assistants'))
        
        sessions = reports.get_assistant_sessions_between(start, end)
        summary = reports.get_assistant_hours_between(start, end)
        
        si = io.StringIO()
        writer = csv.writer(si)
        
        writer.writerow(['PAYROLL STAFF HOURS - DETAILED BREAKDOWN', start, 'to', end])
        writer.writerow([])
        writer.writerow(['Employee Name', 'Day Attended', 'Start Time', 'End Time', 'Hours Logged (HH:MM)'])
        for name, date_only, start_iso, end_iso, duration_sec in sessions:
            start_time = start_iso.split('T')[1][:5] if 'T' in start_iso else start_iso
            end_time = end_iso.split('T')[1][:5] if 'T' in end_iso else end_iso
            # Convert duration to HH:MM format
            hours = int(duration_sec // 3600)
            minutes = int((duration_sec % 3600) // 60)
            duration_hhmm = f"{hours:02d}:{minutes:02d}"
            writer.writerow([name, date_only, start_time, end_time, duration_hhmm])
        
        writer.writerow([])
        writer.writerow(['SUMMARY BY EMPLOYEE'])
        writer.writerow(['Employee Name', 'Total Sessions', 'Total Hours (HH:MM)'])
        for name, sessions_count, total_sec in summary:
            hours = int(total_sec // 3600)
            minutes = int((total_sec % 3600) // 60)
            total_hhmm = f"{hours:02d}:{minutes:02d}"
            writer.writerow([name, sessions_count, total_hhmm])
        
        from flask import Response
        output = si.getvalue().encode('utf-8-sig')
        filename = f"payroll_staff_hours_{start}_to_{end}.csv"
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"'
        }
        return Response(output, mimetype='text/csv', headers=headers)

    # ================================================================
    # Attendance Reports
    # ================================================================

    @app.route('/reports/class-attendance')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def class_attendance_page():
        """HTML page: Class attendance with date pickers and Print to PDF."""
        # Defaults: last 7 days inclusive
        today = datetime.today().date()
        default_start = (today - timedelta(days=7)).isoformat()
        default_end = today.isoformat()

        start_param = request.args.get('start', default_start)
        end_param = request.args.get('end', default_end)
        error = None
        by_day = {}

        # Validate inputs
        try:
            start_date = datetime.strptime(start_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_param, '%Y-%m-%d').date()
        except ValueError:
            error = "Invalid date format; expected YYYY-MM-DD"
            start_date = datetime.strptime(default_start, '%Y-%m-%d').date()
            end_date = datetime.strptime(default_end, '%Y-%m-%d').date()

        if end_date < start_date:
            error = "End date must be on or after start date"

        # Enforce last 30 calendar days
        earliest = today - timedelta(days=30)
        if start_date < earliest or end_date > today or (end_date - start_date).days > 30:
            error = "Dates must be within the last 30 calendar days."

        if not error:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                query = (
                    """
                    SELECT DATE(sess.start_time) as day, s.name
                    FROM sessions AS sess
                    JOIN students AS s ON s.id = sess.student_id
                    WHERE s.active = 1
                      AND DATE(sess.start_time) >= ? AND DATE(sess.start_time) <= ?
                    ORDER BY day, s.name
                    """
                )
                c.execute(query, (start_date.isoformat(), end_date.isoformat()))
                rows = c.fetchall()

            for day, name in rows:
                by_day.setdefault(day, set()).add(name)

        # Convert sets to sorted lists for rendering
        by_day_list = [
            {
                'day': day,
                'names': sorted(list(names)),
                'count': len(names),
            }
            for day, names in sorted(by_day.items())
        ]

        return render_template(
            'reports_class_attendance.html',
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            error=error,
            by_day=by_day_list,
        )

    @app.route('/reports/class-attendance/pdf')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def class_attendance_pdf():
        """Generate PDF: Class attendance by date with active student list."""
        start_param = request.args.get('start')
        end_param = request.args.get('end')
        if not start_param or not end_param:
            return "start and end parameters are required (YYYY-MM-DD)", 400

        try:
            start_date = datetime.strptime(start_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_param, '%Y-%m-%d').date()
        except ValueError:
            return "Invalid date format; expected YYYY-MM-DD", 400

        if end_date < start_date:
            return "end must be on or after start", 400

        today = datetime.today().date()
        earliest = today - timedelta(days=30)
        if start_date < earliest or end_date > today or (end_date - start_date).days > 30:
            return "Range must be within the last 30 calendar days", 400

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            query = """
                SELECT DATE(sess.start_time) as day, s.name
                FROM sessions AS sess
                JOIN students AS s ON s.id = sess.student_id
                WHERE s.active = 1
                  AND DATE(sess.start_time) >= ? AND DATE(sess.start_time) <= ?
                ORDER BY day, s.name
            """
            c.execute(query, (start_date.isoformat(), end_date.isoformat()))
            rows = c.fetchall()

        by_day = {}
        for day, name in rows:
            by_day.setdefault(day, set()).add(name)

        buffer = io.BytesIO()
        canv = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter

        canv.setFont("Helvetica-Bold", 16)
        canv.drawString(inch, height - inch, "Class Attendance Report")
        canv.setFont("Helvetica", 12)
        canv.drawString(inch, height - inch - 0.3*inch, f"Date Range: {start_date} to {end_date}")

        y = height - inch - 0.7*inch
        canv.setFont("Helvetica-Bold", 11)
        canv.drawString(inch, y, "Session Date")
        canv.drawString(inch + 2.2*inch, y, "# Active Students")
        canv.drawString(inch + 4.2*inch, y, "Active Student List")

        y -= 0.3*inch
        canv.setFont("Helvetica", 10)
        for day in sorted(by_day.keys()):
            names = sorted(list(by_day[day]))
            count = len(names)
            names_str = ", ".join(names)

            def wrap_text(txt, limit=90):
                if len(txt) <= limit:
                    return [txt]
                parts, line = [], []
                for w in txt.split(', '):
                    candidate = (', '.join(line+[w])) if line else w
                    if len(candidate) > limit:
                        parts.append(', '.join(line))
                        line = [w]
                    else:
                        line.append(w)
                if line:
                    parts.append(', '.join(line))
                return parts

            lines = wrap_text(names_str, 90)

            canv.drawString(inch, y, str(day))
            canv.drawString(inch + 2.2*inch, y, str(count))
            canv.drawString(inch + 4.2*inch, y, lines[0] if lines else '')
            y -= 0.25*inch

            for extra in lines[1:]:
                canv.drawString(inch + 4.2*inch, y, extra)
                y -= 0.22*inch
                if y < inch:
                    canv.showPage()
                    canv.setFont("Helvetica", 10)
                    y = height - inch

            if y < inch:
                canv.showPage()
                canv.setFont("Helvetica", 10)
                y = height - inch

        canv.save()
        buffer.seek(0)
        filename = f"class_attendance_{start_date}_to_{end_date}.pdf"
        return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)

    @app.route('/reports/student-attendance')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def student_attendance_page():
        """HTML page: Student attendance with student picker, date range, and report table."""
        students = student_manager.get_all_students()
        
        # Defaults: last 7 days and first student (or none if no students)
        today = datetime.today().date()
        default_start = (today - timedelta(days=7)).isoformat()
        default_end = today.isoformat()
        
        sid = request.args.get('sid', type=int)
        start_param = request.args.get('start', default_start)
        end_param = request.args.get('end', default_end)
        error = None
        sessions = []
        student_name = None
        
        # Validate inputs
        try:
            start_date = datetime.strptime(start_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_param, '%Y-%m-%d').date()
        except ValueError:
            error = "Invalid date format; expected YYYY-MM-DD"
            start_date = datetime.strptime(default_start, '%Y-%m-%d').date()
            end_date = datetime.strptime(default_end, '%Y-%m-%d').date()
        
        if end_date < start_date:
            error = "End date must be on or after start date"
        
        # Enforce last 30 calendar days
        earliest = today - timedelta(days=30)
        if start_date < earliest or end_date > today or (end_date - start_date).days > 30:
            error = "Dates must be within the last 30 calendar days."
        
        # Fetch data if sid is provided
        if sid and not error:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                srow = c.execute("SELECT name FROM students WHERE id=?", (sid)).fetchone()
                if srow:
                    student_name = srow[0]
                    q = """
                        SELECT DATE(start_time) as day, TIME(start_time) as start_time, duration
                        FROM sessions
                        WHERE student_id = ?
                          AND DATE(start_time) >= ? AND DATE(start_time) <= ?
                        ORDER BY start_time ASC
                    """
                    c.execute(q, (sid, start_date.isoformat(), end_date.isoformat()))
                    sessions = c.fetchall()
        
        # Format sessions for display
        sessions_list = [
            {
                'date': day,
                'start_time': (start_time[:5] if start_time else "--:--"),
                'duration': f"{int(duration // 3600):02d}:{int((duration % 3600) // 60):02d}" if duration else "00:00"
            }
            for day, start_time, duration in sessions
        ]
        
        return render_template(
            'reports_student_attendance.html',
            students=students,
            sid=sid,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
            student_name=student_name,
            error=error,
            sessions=sessions_list,
        )

    @app.route('/reports/student-attendance/pdf')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def student_attendance_pdf():
        """Generate PDF: Student session history with durations in HH:MM format."""
        sid = request.args.get('sid', type=int)
        start_param = request.args.get('start')
        end_param = request.args.get('end')
        if not sid or not start_param or not end_param:
            return "sid, start and end parameters are required", 400

        try:
            start_date = datetime.strptime(start_param, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_param, '%Y-%m-%d').date()
        except ValueError:
            return "Invalid date format; expected YYYY-MM-DD", 400

        if end_date < start_date:
            return "end must be on or after start", 400

        today = datetime.today().date()
        earliest = today - timedelta(days=30)
        if start_date < earliest or end_date > today or (end_date - start_date).days > 30:
            return "Range must be within the last 30 calendar days", 400

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            srow = c.execute("SELECT name FROM students WHERE id=?", (sid)).fetchone()
            if not srow:
                return "Student not found", 404
            student_name = srow[0]

            q = """
                SELECT DATE(start_time) as day, TIME(start_time) as start_time, duration
                FROM sessions
                WHERE student_id = ?
                  AND DATE(start_time) >= ? AND DATE(start_time) <= ?
                ORDER BY start_time ASC
            """
            c.execute(q, (sid, start_date.isoformat(), end_date.isoformat()))
            sessions = c.fetchall()

        buffer = io.BytesIO()
        canv = canvas.Canvas(buffer, pagesize=letter)
        width, height = letter

        canv.setFont("Helvetica-Bold", 16)
        canv.drawString(inch, height - inch, "Student Attendance Report")
        canv.setFont("Helvetica", 12)
        canv.drawString(inch, height - inch - 0.3*inch, f"Student: {student_name} (ID {sid})")
        canv.drawString(inch, height - inch - 0.55*inch, f"Date Range: {start_date} to {end_date}")

        y = height - inch - 0.9*inch
        canv.setFont("Helvetica-Bold", 11)
        canv.drawString(inch, y, "Session Date")
        canv.drawString(inch + 2.3*inch, y, "Start Time")
        canv.drawString(inch + 3.6*inch, y, "Session Active Time")

        y -= 0.3*inch
        canv.setFont("Helvetica", 10)
        def fmt(sec):
            sec = sec or 0
            h = sec // 3600
            m = (sec % 3600) // 60
            return f"{int(h):02d}:{int(m):02d}"

        for day, start_time, duration in sessions:
            canv.drawString(inch, y, str(day))
            canv.drawString(inch + 2.3*inch, y, (start_time[:5] if start_time else "--:--"))
            canv.drawString(inch + 3.6*inch, y, fmt(duration))
            y -= 0.25*inch
            if y < inch:
                canv.showPage()
                canv.setFont("Helvetica", 10)
                y = height - inch

        if not sessions:
            canv.drawString(inch, y, "No sessions found in the selected range.")

        canv.save()
        buffer.seek(0)
        filename = f"student_{sid}_attendance_{start_date}_to_{end_date}.pdf"
        return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)

    # ================================================================
    # Loaned Books Report
    # ================================================================
    
    @app.route('/reports/loaned-books')
    @require_login
    @require_feature(auth_manager.FEATURE_INSTRUCTOR_REPORTS)
    def loaned_books_report():
        """Display report of all currently loaned books."""
        loaned_books = book_manager.get_loaned_books()
        
        # Group books by student for display
        books_by_student = {}
        for student_name, book_title, checkout_date, student_id in loaned_books:
            if student_name not in books_by_student:
                books_by_student[student_name] = []
            
            # Format the checkout date to be more readable
            try:
                date_obj = datetime.fromisoformat(checkout_date.replace('Z', '+00:00'))
                formatted_date = date_obj.strftime('%Y-%m-%d')
            except:
                formatted_date = checkout_date[:10] if len(checkout_date) >= 10 else checkout_date
            
            books_by_student[student_name].append({
                'title': book_title,
                'checkout_date': formatted_date
            })
        
        return render_template(
            'reports_loaned_books.html',
            books_by_student=books_by_student,
            total_loans=len(loaned_books)
        )
