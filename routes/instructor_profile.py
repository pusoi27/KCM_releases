# routes/instructor_profile.py
from flask import render_template, request, redirect, url_for, flash, jsonify, session, current_app, send_file
from modules import instructor_profile_manager, student_manager, auth_manager, user_identity_manager, schedule_manager
from datetime import datetime, timedelta
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
            
            # Collect weekly hours from single time dropdowns (HH:MM format)
            weekly_hours = {}
            days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            for day in days:
                start = request.form.get(f'{day}_start', '').strip()
                end = request.form.get(f'{day}_end', '').strip()
                weekly_hours[f'{day}_start'] = start
                weekly_hours[f'{day}_end'] = end
            
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
        today = datetime.today().date()
        week_start = today - timedelta(days=today.weekday())

        def normalize_day_name(value):
            token = str(value or '').strip()
            if not token:
                return ''
            lookup = {
                'monday': 'Monday',
                'mon': 'Monday',
                'tuesday': 'Tuesday',
                'tue': 'Tuesday',
                'wednesday': 'Wednesday',
                'wed': 'Wednesday',
                'thursday': 'Thursday',
                'thu': 'Thursday',
                'thur': 'Thursday',
                'thurs': 'Thursday',
                'friday': 'Friday',
                'fri': 'Friday',
                'saturday': 'Saturday',
                'sat': 'Saturday',
                'sunday': 'Sunday',
                'sun': 'Sunday',
            }
            return lookup.get(token.lower(), token)
        
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
            'calendar': {day: {} for day in active_days},
            'day_metadata': {},
            'slot_loading': {day: {} for day in active_days},
            'day_time_slots': {day: set() for day in active_days},
        }

        weekday_lookup = {
            'Monday': 0,
            'Tuesday': 1,
            'Wednesday': 2,
            'Thursday': 3,
            'Friday': 4,
            'Saturday': 5,
            'Sunday': 6,
        }

        for day_name in active_days:
            week_date = week_start + timedelta(days=weekday_lookup.get(day_name, 0))
            is_center_closed = bool(schedule_manager.is_center_closed_date(week_date.isoformat()))
            scheduled_assistants = schedule_manager.get_scheduled_assistants_for_date(week_date.isoformat())
            total_loading = 0
            assistant_lines = []
            for assistant in scheduled_assistants:
                try:
                    assistant_loading = max(0, int(assistant[5] if len(assistant) > 5 else 1))
                except (TypeError, ValueError):
                    assistant_loading = 1
                total_loading += assistant_loading
                assistant_lines.append(f"{assistant[1]} (load {assistant_loading})")

            assistant_summary = ", ".join(assistant_lines) if assistant_lines else "No staff scheduled"
            if is_center_closed:
                assistant_tooltip = (
                    f"{day_name} {week_date.strftime('%b %d')}\n"
                    "Center closed for this date"
                )
            else:
                assistant_tooltip = (
                    f"{day_name} {week_date.strftime('%b %d')}\n"
                    f"Scheduled staff: {assistant_summary}\n"
                    f"Total A/M loading: {total_loading}"
                )

            schedule['day_metadata'][day_name] = {
                'date': week_date.isoformat(),
                'date_label': week_date.strftime('%b %d'),
                'assistants': scheduled_assistants,
                'loading_capacity': 0 if is_center_closed else total_loading,
                'assistant_summary': assistant_summary,
                'assistant_count': len(scheduled_assistants),
                'assistant_tooltip': assistant_tooltip,
                'is_center_closed': is_center_closed,
                'is_open': not is_center_closed,
            }
        
        # Collect virtual students (marked as V=1)
        virtual_students = []
        
        # Get all unique time slots from instructor profile and students
        time_slots_set = set()
        
        if profile:
            days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            day_lookup = {
                'monday': 'Monday',
                'tuesday': 'Tuesday',
                'wednesday': 'Wednesday',
                'thursday': 'Thursday',
                'friday': 'Friday',
                'saturday': 'Saturday',
                'sunday': 'Sunday',
            }
            for day in days:
                start_key = f'{day}_start'
                end_key = f'{day}_end'
                if profile.get(start_key) and profile.get(end_key):
                    # Generate 30-minute slots for this day
                    start_time = profile[start_key]
                    end_time = profile[end_key]
                    slots = generate_time_slots(start_time, end_time)
                    time_slots_set.update(slots)
                    day_name = day_lookup.get(day)
                    if day_name in schedule['day_time_slots']:
                        schedule['day_time_slots'][day_name].update(slots)
        
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
                'email': student[3] if len(student) > 3 else '',
                'el': student[10] if len(student) > 10 else 0,
                'pi': student[11] if len(student) > 11 else 0,
                'v': student[12] if len(student) > 12 else 0,
                'ind': student[22] if len(student) > 22 else 0,
            }

            schedule_entries = []
            seen_days = set()

            raw_schedule_json = student[23] if len(student) > 23 else ''
            if raw_schedule_json:
                try:
                    parsed_schedule = json.loads(raw_schedule_json)
                except (TypeError, ValueError):
                    parsed_schedule = []
                if isinstance(parsed_schedule, list):
                    for entry in parsed_schedule:
                        if not isinstance(entry, dict):
                            continue
                        day = str(entry.get('day') or '').strip()
                        time = str(entry.get('time') or '').strip()
                        if not day or day in seen_days:
                            continue
                        seen_days.add(day)
                        schedule_entries.append({'day': day, 'time': time})
                        if len(schedule_entries) >= 6:
                            break

            if not schedule_entries:
                for day_idx, time_idx in ((13, 14), (15, 16), (24, 25), (26, 27), (28, 29), (30, 31)):
                    day = str(student[day_idx] or '').strip() if len(student) > day_idx else ''
                    time = str(student[time_idx] or '').strip() if len(student) > time_idx else ''
                    if not day or day in seen_days:
                        continue
                    seen_days.add(day)
                    schedule_entries.append({'day': day, 'time': time})
            
            has_scheduled_times = bool(schedule_entries)
            
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

                day_slots = schedule['day_time_slots'].get(day, set())
                if day_slots and time_display not in day_slots:
                    return

                def append_unique(day_name, slot_time, payload):
                    if slot_time not in schedule['calendar'][day_name]:
                        schedule['calendar'][day_name][slot_time] = []
                    existing_ids = {s.get('id') for s in schedule['calendar'][day_name][slot_time]}
                    if payload.get('id') not in existing_ids:
                        schedule['calendar'][day_name][slot_time].append(payload)

                if time_display not in schedule['calendar'][day]:
                    schedule['calendar'][day][time_display] = []
                append_unique(day, time_display, student_data)

                additional_slots = max(0, math.ceil(max(5, duration_minutes) / 30) - 1)
                for step in range(1, additional_slots + 1):
                    current_minutes = time_to_minutes(time_display)
                    next_minutes = current_minutes + (step * 30)
                    next_time_display = minutes_to_time_display(next_minutes)

                    if next_time_display in day_slots:
                        append_unique(day, next_time_display, student_data)
            
            for entry in schedule_entries:
                day_name = normalize_day_name(entry.get('day'))
                time_value = entry.get('time')
                if not day_name or not time_value:
                    continue
                time_display = format_time_display(time_value)
                add_student_to_slot(day_name, time_display, student_data, schedule, duration_minutes=total_study_minutes)

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

        for day in schedule['active_days']:
            day_meta = schedule['day_metadata'].get(day, {})
            day_capacity = day_meta.get('loading_capacity', 0)
            is_open = bool(day_meta.get('is_open', True))
            valid_day_slots = schedule['day_time_slots'].get(day, set())
            for time_slot in schedule['time_slots']:
                slot_students = schedule['calendar'].get(day, {}).get(time_slot, []) or []
                slot_is_within_hours = time_slot in valid_day_slots
                occupied_count = sum(
                    1
                    for student in slot_students
                    if student.get('el') or student.get('pi') or student.get('v')
                )
                over_capacity = max(0, occupied_count - day_capacity)
                if is_open and slot_is_within_hours:
                    free_slots = max(0, day_capacity - occupied_count)
                    slot_capacity = day_capacity
                    slot_is_open = True
                else:
                    free_slots = 0
                    slot_capacity = 0
                    slot_is_open = False
                schedule['slot_loading'][day][time_slot] = {
                    'capacity': slot_capacity,
                    'occupied': occupied_count,
                    'free': free_slots,
                    'over_capacity': over_capacity,
                    'is_open': slot_is_open,
                    'in_day_hours': slot_is_within_hours,
                }
        
        total_students = len([
            s for s in students
            if (
                (len(s) > 14 and s[14])
                or (len(s) > 16 and s[16])
                or (len(s) > 25 and s[25])
                or (len(s) > 27 and s[27])
                or (len(s) > 29 and s[29])
                or (len(s) > 31 and s[31])
            )
        ])
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
