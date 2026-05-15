# routes/instructor_profile.py
from flask import render_template, request, redirect, url_for, flash, jsonify, session, current_app, send_file
from modules import instructor_profile_manager, student_manager, auth_manager, user_identity_manager
from datetime import datetime
from routes.auth import require_login
import math
import json
import os


TIMEZONE_OPTIONS = [
    "UTC-10 (Honolulu)",
    "UTC-9 (Anchorage)",
    "UTC-8 (Los Angeles)",
    "UTC-7 (Denver)",
    "UTC-6 (Chicago)",
    "UTC-5 (New York)",
    "UTC-4 (Santiago)",
    "UTC+0 (London)",
    "UTC+1 (Berlin)",
    "UTC+2 (Athens)",
    "UTC+3 (Nairobi)",
    "UTC+5:30 (New Delhi)",
    "UTC+8 (Singapore)",
    "UTC+9 (Tokyo)",
    "UTC+10 (Sydney)",
]


def register_instructor_profile_routes(app):
    """Register instructor profile CRUD routes."""
    
    @app.route("/instructor/profile")
    @require_login
    def instructor_profile():
        """Display the instructor profile page"""
        profile = instructor_profile_manager.get_instructor_profile()
        return render_template("instructor_profile.html", profile=profile)

    @app.route("/instructor/profile/edit", methods=["GET", "POST"])
    @require_login
    def instructor_profile_edit():
        """Edit or create instructor profile"""
        profile = instructor_profile_manager.get_instructor_profile()
        setup_mode = request.args.get('setup', '').strip() == '1' or request.form.get('setup_mode', '').strip() == '1'
        active_email = user_identity_manager.resolve_active_email(session.get('user_email'))
        email_prefill = (profile.get('email') if profile else '') or active_email or ''
        if not active_email:
            active_email = user_identity_manager.resolve_active_email()
        
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            if not email and active_email:
                email = active_email
            phone = request.form.get("phone", "").strip()
            center_location = request.form.get("center_location", "").strip()
            center_address = request.form.get("center_address", "").strip()
            center_time_zone = request.form.get("center_time_zone", "").strip()
            center_hours = request.form.get("center_hours", "").strip()
            
            # Collect weekly hours from separate hour and minute dropdowns
            weekly_hours = {}
            days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            for day in days:
                # Combine hour and minute for start time
                start_hour = request.form.get(f'{day}_start_hour', '').strip()
                start_min = request.form.get(f'{day}_start_min', '').strip()
                if start_hour and start_min:
                    weekly_hours[f'{day}_start'] = f"{start_hour}:{start_min}"
                else:
                    weekly_hours[f'{day}_start'] = ''
                
                # Combine hour and minute for end time
                end_hour = request.form.get(f'{day}_end_hour', '').strip()
                end_min = request.form.get(f'{day}_end_min', '').strip()
                if end_hour and end_min:
                    weekly_hours[f'{day}_end'] = f"{end_hour}:{end_min}"
                else:
                    weekly_hours[f'{day}_end'] = ''
            
            if not name:
                flash("Instructor name is required.", "error")
                return render_template(
                    "instructor_profile_form.html",
                    profile=profile,
                    email_prefill=email_prefill,
                    action="Edit" if profile else "Create",
                    timezone_options=TIMEZONE_OPTIONS,
                    setup_mode=setup_mode,
                )

            has_any_class_hours = any(
                weekly_hours.get(f'{day}_start') and weekly_hours.get(f'{day}_end')
                for day in days
            )
            if not has_any_class_hours:
                flash("Please add center class hours for at least one day before continuing.", "error")
                return render_template(
                    "instructor_profile_form.html",
                    profile=profile,
                    email_prefill=email_prefill,
                    action="Edit" if profile else "Create",
                    timezone_options=TIMEZONE_OPTIONS,
                    setup_mode=setup_mode,
                )
            
            if profile:
                # Update existing profile
                instructor_profile_manager.update_instructor_profile(
                    profile['id'],
                    name,
                    email,
                    phone,
                    center_location,
                    center_address,
                    center_time_zone,
                    center_hours,
                    weekly_hours
                )
                if user_identity_manager.is_valid_email(email):
                    user_identity_manager.save_email(email)
                flash("Instructor profile updated successfully.", "success")
            else:
                # Create new profile
                instructor_profile_manager.create_instructor_profile(
                    name,
                    email,
                    phone,
                    center_location,
                    center_address,
                    center_time_zone,
                    center_hours,
                    weekly_hours
                )
                if user_identity_manager.is_valid_email(email):
                    user_identity_manager.save_email(email)
                flash("Instructor profile created successfully.", "success")
            
            if setup_mode:
                session['setup_complete_once'] = True
                return redirect(url_for("setup_requirements"))
            return redirect(url_for("instructor_profile"))
        
        action = "Edit" if profile else "Create"
        if active_email and profile and not profile.get('email'):
            profile['email'] = active_email
        return render_template(
            "instructor_profile_form.html",
            profile=profile,
            email_prefill=email_prefill,
            action=action,
            timezone_options=TIMEZONE_OPTIONS,
            setup_mode=setup_mode,
        )

    @app.route("/api/instructor/profile", methods=["GET"])
    @require_login
    def api_get_instructor_profile():
        """API endpoint to get instructor profile (for AJAX requests)

        Always returns HTTP 200 with a `success` flag so frontend fetch won't
        throw on non-200 responses.
        """
        profile = instructor_profile_manager.get_instructor_profile()
        if profile:
            return jsonify({
                'success': True,
                'profile': profile,
            })
        return jsonify({
            'success': False,
            'profile': None,
            'error': 'Instructor profile not found'
        })

    @app.route('/instructor/dev-trace')
    @require_login
    def instructor_dev_trace():
        """Download the per-launch development trace file."""
        if not current_app.config.get('DEV_TRACE_ENABLED'):
            flash('Development trace logging is disabled.', 'warning')
            return redirect(url_for('instructor_profile'))

        trace_path = current_app.config.get('DEV_TRACE_FILE_PATH', '')
        if not trace_path or not os.path.exists(trace_path):
            flash('Trace file not found for this app launch.', 'warning')
            return redirect(url_for('instructor_profile'))

        return send_file(
            trace_path,
            mimetype='text/plain; charset=utf-8',
            as_attachment=True,
            download_name=os.path.basename(trace_path),
            max_age=0,
        )

    @app.route('/instructor/dev-trace-folder')
    @require_login
    def instructor_dev_trace_folder():
        """Show available development trace files (newest first)."""
        if not current_app.config.get('DEV_TRACE_ENABLED'):
            flash('Development trace logging is disabled.', 'warning')
            return redirect(url_for('instructor_profile'))

        trace_dir = os.path.join(current_app.root_path, 'trace_logs')
        os.makedirs(trace_dir, exist_ok=True)

        trace_files = []
        try:
            for fname in os.listdir(trace_dir):
                if not fname.lower().endswith('.txt'):
                    continue
                fpath = os.path.join(trace_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                stat = os.stat(fpath)
                trace_files.append({
                    'name': fname,
                    'size_bytes': int(stat.st_size),
                    'modified_at': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'modified_ts': float(stat.st_mtime),
                    'is_current': fname == os.path.basename(current_app.config.get('DEV_TRACE_FILE_PATH', '') or ''),
                })
        except Exception as exc:
            flash(f'Unable to read trace folder: {exc}', 'danger')
            return redirect(url_for('instructor_profile'))

        trace_files.sort(key=lambda item: item['modified_ts'], reverse=True)
        return render_template(
            'dev_trace_folder.html',
            trace_files=trace_files,
            trace_dir=trace_dir,
        )

    @app.route('/instructor/dev-trace-file/<path:filename>')
    @require_login
    def instructor_dev_trace_file(filename):
        """Download a specific development trace file from trace_logs."""
        if not current_app.config.get('DEV_TRACE_ENABLED'):
            flash('Development trace logging is disabled.', 'warning')
            return redirect(url_for('instructor_profile'))

        safe_name = os.path.basename(filename or '')
        if not safe_name.lower().endswith('.txt'):
            flash('Invalid trace file.', 'danger')
            return redirect(url_for('instructor_dev_trace_folder'))

        trace_dir = os.path.join(current_app.root_path, 'trace_logs')
        file_path = os.path.join(trace_dir, safe_name)
        if not os.path.exists(file_path):
            flash('Trace file not found.', 'warning')
            return redirect(url_for('instructor_dev_trace_folder'))

        return send_file(
            file_path,
            mimetype='text/plain; charset=utf-8',
            as_attachment=True,
            download_name=safe_name,
            max_age=0,
        )

    @app.route('/instructor/dev-trace-folder/cleanup', methods=['POST'])
    @require_login
    def instructor_dev_trace_cleanup():
        """Delete older trace files while keeping the newest N files."""
        if not current_app.config.get('DEV_TRACE_ENABLED'):
            flash('Development trace logging is disabled.', 'warning')
            return redirect(url_for('instructor_profile'))

        keep_count_raw = (request.form.get('keep_count') or '20').strip()
        try:
            keep_count = max(0, int(keep_count_raw))
        except ValueError:
            keep_count = 20

        trace_dir = os.path.join(current_app.root_path, 'trace_logs')
        os.makedirs(trace_dir, exist_ok=True)
        current_trace_name = os.path.basename(current_app.config.get('DEV_TRACE_FILE_PATH', '') or '')

        trace_files = []
        for fname in os.listdir(trace_dir):
            if not fname.lower().endswith('.txt'):
                continue
            fpath = os.path.join(trace_dir, fname)
            if not os.path.isfile(fpath):
                continue
            stat = os.stat(fpath)
            trace_files.append((fname, float(stat.st_mtime), fpath))

        trace_files.sort(key=lambda item: item[1], reverse=True)
        keep_names = {name for name, _, _ in trace_files[:keep_count]}
        if current_trace_name:
            keep_names.add(current_trace_name)

        deleted = 0
        failed = 0
        for name, _mtime, fpath in trace_files:
            if name in keep_names:
                continue
            try:
                os.remove(fpath)
                deleted += 1
            except Exception:
                failed += 1

        if failed:
            flash(f'Deleted {deleted} old trace file(s); {failed} could not be deleted.', 'warning')
        else:
            flash(f'Deleted {deleted} old trace file(s); kept latest {keep_count}.', 'success')

        return redirect(url_for('instructor_dev_trace_folder'))

    @app.route("/instructor/calendar")
    @require_login
    def center_calendar():
        """Display the center calendar with student schedules"""
        profile = instructor_profile_manager.get_instructor_profile()
        students = student_manager.get_all_students()
        
        # Determine which days have class hours
        active_days = []
        day_mapping = [
            ('Monday', 'monday'),
            ('Tuesday', 'tuesday'),
            ('Wednesday', 'wednesday'),
            ('Thursday', 'thursday'),
            ('Friday', 'friday'),
            ('Saturday', 'saturday'),
            ('Sunday', 'sunday')
        ]
        
        if profile:
            for day_name, day_key in day_mapping:
                start_key = f'{day_key}_start'
                end_key = f'{day_key}_end'
                if profile.get(start_key) and profile.get(end_key):
                    active_days.append(day_name)
        
        # Build the calendar structure
        schedule = {
            'time_slots': [],
            'active_days': active_days,
            'calendar': {day: {} for day in active_days}
        }
        
        # Collect virtual students (marked as V=1)
        virtual_students = []
        
        # Get all unique time slots from instructor profile and students
        time_slots_set = set()
        
        if profile:
            days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            for day in days:
                start_key = f'{day}_start'
                end_key = f'{day}_end'
                if profile.get(start_key) and profile.get(end_key):
                    # Generate 30-minute slots for this day
                    start_time = profile[start_key]
                    end_time = profile[end_key]
                    slots = generate_time_slots(start_time, end_time)
                    time_slots_set.update(slots)
        
        # Sort time slots
        schedule['time_slots'] = sorted(list(time_slots_set), key=lambda t: time_to_minutes(t))
        
        # Place students in calendar
        for student in students:
            total_study_minutes = 30
            if len(student) > 19 and student[19]:
                try:
                    total_study_minutes = max(5, int(student[19]))
                except (TypeError, ValueError):
                    total_study_minutes = 30

            subjects_display = student[2] if student[2] else 'N/A'
            if len(student) > 17 and student[17]:
                try:
                    parsed_subjects = [str(s).strip() for s in json.loads(student[17]) if str(s).strip()]
                    if parsed_subjects:
                        subjects_display = ", ".join(parsed_subjects)
                except (TypeError, ValueError):
                    pass

            student_data = {
                'id': student[0],
                'name': student[1],
                'subject': subjects_display,
                'email': student[4] if len(student) > 4 else '',
                'el': student[10] if len(student) > 10 else 0,
                'pi': student[11] if len(student) > 11 else 0,
                'v': student[12] if len(student) > 12 else 0,
            }
            
            # Check if student has scheduled times
            has_day1 = len(student) > 13 and student[13]
            has_day2 = len(student) > 15 and student[15]
            has_scheduled_times = has_day1 or has_day2
            
            # Check if student is virtual
            is_virtual = student[12] if len(student) > 12 else 0
            
            # If virtual with NO scheduled times, add to virtual students list instead of calendar
            if is_virtual and not has_scheduled_times:
                virtual_students.append(student_data)
                continue
            
            # Helper function to add student to a specific day/time with additional slots based on study duration.
            def add_student_to_slot(day, time_display, student_data, schedule, duration_minutes=30):
                if day not in schedule['calendar']:
                    return
                if time_display not in schedule['calendar'][day]:
                    schedule['calendar'][day][time_display] = []
                schedule['calendar'][day][time_display].append(student_data)

                additional_slots = max(0, math.ceil(max(5, duration_minutes) / 30) - 1)
                for step in range(1, additional_slots + 1):
                    current_minutes = time_to_minutes(time_display)
                    next_minutes = current_minutes + (step * 30)
                    next_time_display = minutes_to_time_display(next_minutes)

                    if next_time_display in schedule['time_slots']:
                        if next_time_display not in schedule['calendar'][day]:
                            schedule['calendar'][day][next_time_display] = []
                        schedule['calendar'][day][next_time_display].append(student_data)
            
            # Add to Day 1
            if len(student) > 13 and student[13]:  # day1
                day1 = student[13]
                time1 = student[14] if len(student) > 14 else None
                if time1:
                    time_display = format_time_display(time1)
                    add_student_to_slot(day1, time_display, student_data, schedule, duration_minutes=total_study_minutes)
            
            # Add to Day 2
            if len(student) > 15 and student[15]:  # day2
                day2 = student[15]
                time2 = student[16] if len(student) > 16 else None
                if time2:
                    time_display = format_time_display(time2)
                    add_student_to_slot(day2, time_display, student_data, schedule, duration_minutes=total_study_minutes)

        # Order students within each slot: EL first, then PI, then the rest
        for day in schedule['calendar']:
            for time_slot in schedule['calendar'][day]:
                schedule['calendar'][day][time_slot].sort(
                    key=lambda s: (
                        0 if s.get('el') else 1,
                        0 if s.get('pi') else 1,
                        s.get('name', '')
                    )
                )
        
        total_students = len([s for s in students if (len(s) > 14 and s[14]) or (len(s) > 16 and s[16])])
        schedule['virtual_students'] = virtual_students
        
        return render_template("center_calendar.html", schedule=schedule, total_students=total_students)


def generate_time_slots(start_time, end_time):
    """Generate 30-minute time slots between start and end time.
    Excludes the final slot (at end_time) since students implicitly cannot start at that time.
    """
    slots = []
    start_minutes = time_to_minutes(start_time)
    end_minutes = time_to_minutes(end_time)
    
    current = start_minutes
    while current < end_minutes:  # Changed from <= to < to exclude the final slot
        slots.append(minutes_to_time_display(current))
        current += 30
    
    return slots


def time_to_minutes(time_str):
    """Convert HH:MM or HH:MM AM/PM to minutes since midnight
    Default to PM when no AM/PM marker is present (since all class hours are PM)
    """
    if not time_str or ':' not in time_str:
        return 0
    
    # Extract AM/PM suffix if present
    is_pm = ' PM' in time_str
    is_am = ' AM' in time_str
    
    # Remove AM/PM suffix if present
    time_str = time_str.replace(' PM', '').replace(' AM', '').strip()
    parts = time_str.split(':')
    hour = int(parts[0])
    minute = int(parts[1])
    
    # Convert 12-hour to 24-hour
    if is_pm or is_am:
        # Explicit AM/PM marker present
        if is_pm and hour != 12:
            hour += 12
        elif is_am and hour == 12:
            hour = 0
    else:
        # No AM/PM marker - default to PM for times <= 12 (since all class hours are PM)
        if hour <= 12:
            if hour != 12:
                hour += 12
        # If hour > 12, assume it's already in 24-hour format (shouldn't happen, but handle it)
    
    return hour * 60 + minute


def minutes_to_time_display(minutes):
    """Convert minutes to display format (12-hour PM only - no AM times)"""
    hour = minutes // 60
    minute = minutes % 60
    # Convert 24-hour to 12-hour format
    display_hour = hour if hour <= 12 else hour - 12
    if display_hour == 0:
        display_hour = 12
    # All times are PM (no AM times in the system)
    return f"{display_hour}:{minute:02d} PM"


def format_time_display(time_str):
    """Format time from 24-hour to 12-hour PM format (no AM times)"""
    if not time_str or ':' not in time_str:
        return time_str
    parts = time_str.split(':')
    hour = int(parts[0])
    minute = parts[1]
    # Convert 24-hour to 12-hour format
    display_hour = hour if hour <= 12 else hour - 12
    if display_hour == 0:
        display_hour = 12
    # All times are PM (no AM times in the system)
    return f"{display_hour}:{minute} PM"
