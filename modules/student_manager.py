#*****************************
#student_manager.py   ver 04--------------
#*****************************

import sqlite3, csv, os, json
from modules.database import DB_PATH

MAX_SUBJECTS = 3
MAX_SCHEDULE_DAYS = 7


def _loads_json_list(raw_value, default=None):
    """Safely decode a JSON list value."""
    if default is None:
        default = []
    if not raw_value:
        return list(default)
    try:
        data = json.loads(raw_value)
    except (TypeError, ValueError):
        return list(default)
    return data if isinstance(data, list) else list(default)


def _classification_label(el=0, pi=0, paper_ws=0, v=0):
    """Return the primary classification label for a student."""
    if int(bool(el)):
        return "Assisted"
    if int(bool(pi)):
        return "Monitored"
    if int(bool(paper_ws)):
        return "Independent"
    if int(bool(v)):
        return "Virtual"
    return "Monitored"


def _normalize_schedule_entries(schedule_json, day1='', day1_time='', day2='', day2_time=''):
    """Return normalized schedule entries from JSON with legacy fallback."""
    entries = []
    seen = set()

    for entry in _loads_json_list(schedule_json):
        if not isinstance(entry, dict):
            continue
        day = str(entry.get('day') or '').strip()
        time = str(entry.get('time') or '').strip()
        if not day or day in seen:
            continue
        seen.add(day)
        entries.append({'day': day, 'time': time})

    if not entries:
        for day, time in ((day1, day1_time), (day2, day2_time)):
            day = str(day or '').strip()
            if not day or day in seen:
                continue
            seen.add(day)
            entries.append({'day': day, 'time': str(time or '').strip()})

    return entries[:MAX_SCHEDULE_DAYS]


def _build_student_database_row(row):
    """Convert a database row into the student database view model."""
    subjects = [str(s).strip() for s in _loads_json_list(row[10]) if str(s or '').strip()]
    if not subjects and row[2]:
        subjects = [str(row[2]).strip()]

    subject_slots = [""] * MAX_SUBJECTS
    for idx, subject_name in enumerate(subjects[:MAX_SUBJECTS]):
        subject_slots[idx] = subject_name

    schedule_entries = _normalize_schedule_entries(
        row[11],
        day1=row[12],
        day1_time=row[13],
        day2=row[14],
        day2_time=row[15],
    )
    schedule_slots = [None] * MAX_SCHEDULE_DAYS
    for idx, entry in enumerate(schedule_entries[:MAX_SCHEDULE_DAYS]):
        schedule_slots[idx] = entry

    return {
        'id': row[0],
        'name': row[1],
        'subject': row[2],
        'email': row[3],
        'phone': row[4],
        'active': bool(row[5]),
        'book_loaned': bool(row[6]),
        'paper_ws': bool(row[7]),
        'el': bool(row[8]),
        'pi': bool(row[9]),
        'subjects': subjects,
        'subject_slots': subject_slots,
        'classification': _classification_label(el=row[8], pi=row[9], paper_ws=row[7], v=row[16]),
        'virtual': bool(row[16]),
        'schedule': schedule_entries,
        'schedule_slots': schedule_slots,
        'qr_filename': f"student_{row[0]}.png",
    }


def get_student_database_rows(active=1):
    """Get student rows tailored for the Student Database screen only."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT
                s.id,
                s.name,
                COALESCE(s.subject, ''),
                COALESCE(s.email, ''),
                COALESCE(s.phone, ''),
                s.active,
                s.book_loaned,
                s.paper_ws,
                s.el,
                s.pi,
                COALESCE(s.subjects_json, '[]'),
                COALESCE(s.schedule_json, ''),
                COALESCE(s.day1, ''),
                COALESCE(s.day1_time, ''),
                COALESCE(s.day2, ''),
                COALESCE(s.day2_time, ''),
                s.v
            FROM students s
            WHERE s.active = ?
            ORDER BY s.name
            """,
            (active,),
        )
        return [_build_student_database_row(row) for row in c.fetchall()]


def safe_int(value, default=0):
    """Safely convert value to int, returning default if empty or invalid."""
    try:
        val = str(value).strip()
        if not val:
            return default
        return int(val)
    except (ValueError, TypeError):
        return default


def normalize_subject_entries(subjects, minutes):
    """Normalize subjects and their durations.

    Returns:
        tuple[list[str], list[int], int]: (subjects, minutes, total_minutes)
    """
    cleaned_subjects = []
    cleaned_minutes = []

    for idx, raw_subj in enumerate(subjects or []):
        subj = str(raw_subj or "").strip()
        if not subj:
            continue
        minute_raw = minutes[idx] if idx < len(minutes or []) else 30
        minute_val = max(5, safe_int(minute_raw, default=30))
        cleaned_subjects.append(subj)
        cleaned_minutes.append(minute_val)

    if not cleaned_subjects:
        cleaned_subjects = ["Math"]
        cleaned_minutes = [30]

    cleaned_subjects = cleaned_subjects[:MAX_SUBJECTS]
    cleaned_minutes = cleaned_minutes[:MAX_SUBJECTS]

    total_minutes = sum(cleaned_minutes) if cleaned_minutes else 30
    return cleaned_subjects, cleaned_minutes, total_minutes


def get_all_students():
    """Get all active students with their information for a specific user.
    
    Args:
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # Get only active student data for this owner
        c.execute("""
             SELECT s.id, s.name, s.subject, s.level, s.email, s.phone, '' AS legacy_contact, s.active, s.book_loaned, s.paper_ws,
                 s.el, s.pi, s.v, s.day1, s.day1_time, s.day2, s.day2_time, s.subjects_json, s.subject_minutes_json, s.total_study_minutes,
                 COALESCE(s.photo, '') AS photo
            FROM students s
            WHERE s.active = 1
            ORDER BY s.name
        """, ())
        return c.fetchall()


def get_student(student_id):
    """Get a single student by ID, with ownership check.
    
    Args:
        student_id: Student ID to retrieve
    """
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        row = c.execute("""
            SELECT id,name,subject,email,phone,'' AS legacy_contact,active,book_loaned,paper_ws,
                   el,pi,v,day1,day2,day1_time,day2_time,subjects_json,subject_minutes_json,total_study_minutes,
                   COALESCE(photo,'') AS photo,
                   COALESCE(schedule_json,'') AS schedule_json
            FROM students WHERE id=?
        """, (student_id,)).fetchone()
        return row


def get_student_static_profile(student_id):
    """Get a single student by ID as a dictionary."""
    row = get_student(student_id)
    if not row:
        return None
    return {
        'id': row[0],
        'name': row[1],
        'subject': row[2],
        'email': row[3],
        'phone': row[4],
        'active': row[6],
        'book_loaned': row[7],
        'paper_ws': row[8],
        'el': row[9],
        'pi': row[10],
        'v': row[11],
        'day1': row[12],
        'day2': row[13],
        'day1_time': row[14],
        'day2_time': row[15],
        'subjects': json.loads(row[16] or '[]') if len(row) > 16 else ([row[2]] if row[2] else []),
        'subject_minutes': json.loads(row[17] or '[]') if len(row) > 17 else ([30] if row[2] else []),
        'total_study_minutes': int(row[18] or 30) if len(row) > 18 else 30,
        'photo': str(row[19] or '') if len(row) > 19 else '',
    }


def set_student_photo(student_id, photo_filename):
    """Set or clear a student's photo filename."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE students SET photo=? WHERE id=?",
            (photo_filename or '', student_id),
        )
        conn.commit()



def add_student(name, subject, email, phone, book_loaned=0, paper_ws=0, el=0, pi=0, v=0, day1="", day2="", day1_time="", day2_time="", subjects=None, subject_minutes=None, schedule_json=""):
    """Add a new student to the database and automatically generate QR code.
    
    Args:
    """
    subjects_list, minutes_list, total_minutes = normalize_subject_entries(
        subjects if subjects is not None else [subject],
        subject_minutes if subject_minutes is not None else [30],
    )
    primary_subject = subjects_list[0]

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO students
            (name,subject,subjects_json,subject_minutes_json,total_study_minutes,email,phone,active,book_loaned,paper_ws,el,pi,v,day1,day2,day1_time,day2_time,schedule_json)
            VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                primary_subject,
                json.dumps(subjects_list),
                json.dumps(minutes_list),
                total_minutes,
                email,
                phone,
                int(bool(book_loaned)),
                int(bool(paper_ws)),
                int(bool(el)),
                int(bool(pi)),
                int(bool(v)),
                day1,
                day2,
                day1_time,
                day2_time,
                schedule_json,
            ))
        student_id = c.lastrowid
        conn.commit()
    
    # Automatically generate QR code for the new student
    try:
        from modules import qr_generator
        qr_data = f"ID:{student_id}\nName:{name}"
        qr_generator.generate_qr(qr_data, f"student_{student_id}")
    except Exception as e:
        print(f"Warning: Failed to generate QR code for student {student_id}: {e}")
    
    return student_id



def update_student(sid, name, email, phone, subject="", book_loaned=0, paper_ws=0, el=0, pi=0, v=0, day1="", day2="", day1_time="", day2_time="", subjects=None, subject_minutes=None, schedule_json=""):
    """Update an existing student's information with ownership check."""
    subjects_list, minutes_list, total_minutes = normalize_subject_entries(
        subjects if subjects is not None else [subject],
        subject_minutes if subject_minutes is not None else [30],
    )
    primary_subject = subjects_list[0]

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""UPDATE students SET name=?,subject=?,subjects_json=?,subject_minutes_json=?,total_study_minutes=?,email=?,phone=?,book_loaned=?,paper_ws=?,el=?,pi=?,v=?,day1=?,day2=?,day1_time=?,day2_time=?,schedule_json=? WHERE id=?""",
                  (
                      name,
                      primary_subject,
                      json.dumps(subjects_list),
                      json.dumps(minutes_list),
                      total_minutes,
                      email,
                      phone,
                      int(bool(book_loaned)),
                      int(bool(paper_ws)),
                      int(bool(el)),
                      int(bool(pi)),
                      int(bool(v)),
                      day1,
                      day2,
                      day1_time,
                      day2_time,
                      schedule_json,
                      sid,
                  ))
        conn.commit()


def delete_student(sid):
    """Soft delete: mark student as inactive instead of hard delete with ownership check."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE students SET active=0 WHERE id=?", (sid,))
        conn.commit()


def permanent_delete_student(sid):
    """Permanently delete student from database (hard delete) with ownership check."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM students WHERE id=?", (sid,))
        conn.commit()


def get_deleted_students():
    """Get all deleted/inactive students for a specific user."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT s.id, s.name, s.subject, s.level, s.email, s.phone, '' AS legacy_contact, s.active, s.book_loaned, s.paper_ws,
                   s.math_goal, s.math_ws_per_week, s.reading_goal, s.reading_ws_per_week,
                   s.el, s.pi, s.v, s.day1, s.day1_time, s.day2, s.day2_time
            FROM students s
            WHERE s.active = 0
            ORDER BY s.name
        """, ())
        return c.fetchall()


def reactivate_student(sid):
    """Reactivate a deleted/inactive student with ownership check."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE students SET active=1 WHERE id=?", (sid,))
        conn.commit()


def import_csv(file_path):
    """Import students from CSV with ownership assignment.
    
    Args:
        file_path: Path to CSV file
    """
    if not os.path.exists(file_path):
        return {"added": 0, "updated": 0, "deleted": 0}
    added=0
    updated=0
    deleted=0
    csv_names=set()  # Track names from CSV
    
    with sqlite3.connect(DB_PATH) as conn, open(file_path,newline="",encoding="utf-8-sig") as f:
        reader=csv.DictReader(f)
        
        # Debug: Log column headers
        first_row = True
        for row in reader:
            if first_row:
                print(f"CSV Columns: {list(row.keys())}")
                first_row = False
            
            name=row.get("name") or ""
            if not name.strip(): continue
            csv_names.add(name.lower().strip())  # Store lowercase name for matching
            
            email=row.get("email") or ""
            phone=row.get("phone") or ""
            subject=row.get("subject") or ""
            math_goal=row.get("math_goal") or ""
            # Map CSV column Math/WS to database column math_ws_per_week
            math_ws=safe_int(row.get("Math/WS") or row.get("math/ws") or "0", default=0)
            reading_goal=row.get("reading_goal") or ""
            # Map CSV column Reading/WS to database column reading_ws_per_week
            reading_ws=safe_int(row.get("Reading/WS") or row.get("reading/ws") or "0", default=0)
            print(f"Student: {name}, Math/WS: {math_ws}, Reading/WS: {reading_ws}")
            
            if subject.strip() not in {"S1", "S2"}:
                return {"error": "CSV import failed: subject must be S1 or S2 for every student."}
            
            # Check if student exists (owned by this user)
            student_record=conn.execute("SELECT id FROM students WHERE LOWER(TRIM(name))=LOWER(?)",(name.strip(),)).fetchone()
            
            if student_record:
                # UPDATE existing student - set all fields from CSV
                student_id = student_record[0]
                print(f"UPDATING student ID {student_id}: {name}")
                conn.execute("""UPDATE students SET name=?, subject=?, email=?, phone=?, math_goal=?, math_ws_per_week=?, reading_goal=?, reading_ws_per_week=?, active=1 WHERE id=?"""
                             ,(name,subject,email,phone,math_goal,math_ws,reading_goal,reading_ws,student_id))
                updated+=1
            else:
                # INSERT new student with                print(f"INSERTING new student: {name}")
                conn.execute("""INSERT INTO students(name,subject,email,phone,active,math_goal,math_ws_per_week,reading_goal,reading_ws_per_week)
                                VALUES(?,?,?,?,1,?,?,?,?,?)""",(name,subject,email,phone,math_goal,math_ws,reading_goal,reading_ws))
                added+=1
        
        # PERMANENTLY delete students not in CSV
        cursor=conn.cursor()
        cursor.execute("SELECT id, name FROM students WHERE active=1")
        db_students=cursor.fetchall()
        print(f"CSV names: {csv_names}")
        for student_id, student_name in db_students:
            student_name_lower = student_name.lower().strip()
            if student_name_lower not in csv_names:
                conn.execute("DELETE FROM students WHERE id=?", (student_id,))
                deleted+=1
                print(f"Permanently deleting: {student_name}")
        
        conn.commit()
    return {"added": added, "updated": updated, "deleted": deleted}

def export_csv(path):
    """Export all active students to CSV with proper alignment of headers and data."""
    data=get_all_students()
    # Headers must match the order of columns returned by get_all_students()
    # get_all_students returns: id, name, subject, level, email, phone, legacy_contact, active,
    # book_loaned, paper_ws, math_goal, math_ws_per_week, reading_goal, reading_ws_per_week,
    # el, pi, v, day1, day1_time, day2, day2_time
    headers=[
        "ID",
        "Student Name",
        "Subject",
        "Level",
        "Email",
        "Phone",
        "Active",
        "Book Loaned",
        "Paper Worksheets",
        "Math Goal",
        "Math Worksheets/Week",
        "Reading Goal",
        "Reading Worksheets/Week",
        "Early Learner",
        "Primary Instruction",
        "Virtual",
        "Day 1",
        "Day 1 Time",
        "Day 2",
        "Day 2 Time"
    ]
    with open(path,"w",newline="",encoding="utf-8") as f:
        writer=csv.writer(f)
        writer.writerow(headers)
        writer.writerows([list(row[:6]) + list(row[7:]) for row in data])


def find_duplicates_by_name(name):
    """Find all students with a given name (case-insensitive, whitespace-trimmed)."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, name, email, phone, subject, day1, day1_time, day2, day2_time
            FROM students
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
            ORDER BY id
        """, (name,))
        return c.fetchall()


def get_duplicate_names():
    """Get all student names that appear more than once in the student list."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT LOWER(TRIM(name)) as name_key, COUNT(*) as count
            FROM students
            WHERE active = 1
            GROUP BY LOWER(TRIM(name))
            HAVING COUNT(*) > 1
            ORDER BY count DESC, name_key
        """, ())
        return c.fetchall()


def get_duplicate_name_count():
    """Return how many distinct duplicate names exist among active students."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        row = c.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT LOWER(TRIM(name))
                FROM students
                WHERE active = 1
                GROUP BY LOWER(TRIM(name))
                HAVING COUNT(*) > 1
            )
            """,
            (),
        ).fetchone()
        return int(row[0] or 0) if row else 0


def get_duplicate_summary():
    """Get a detailed summary of all duplicate names with their student information."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        rows = c.execute(
            """
            WITH duplicate_names AS (
                SELECT LOWER(TRIM(name)) AS name_key, COUNT(*) AS dup_count
                FROM students
                WHERE active = 1
                GROUP BY LOWER(TRIM(name))
                HAVING COUNT(*) > 1
            )
            SELECT
                d.name_key,
                d.dup_count,
                s.id,
                s.name,
                s.email,
                s.phone,
                s.subject,
                s.day1,
                s.day1_time,
                s.day2,
                s.day2_time,
                s.el,
                s.pi,
                s.v
            FROM duplicate_names d
            JOIN students s
              ON LOWER(TRIM(s.name)) = d.name_key
             AND s.active = 1
            ORDER BY d.dup_count DESC, d.name_key, s.id
            """,
            (),
        ).fetchall()

    summary = []
    current_key = None
    current_block = None

    for row in rows:
        name_key = row[0]
        dup_count = row[1]
        if current_key != name_key:
            current_key = name_key
            current_block = {
                'name': row[3],
                'count': dup_count,
                'students': [],
            }
            summary.append(current_block)

        current_block['students'].append(
            {
                'id': row[2],
                'name': row[3],
                'email': row[4],
                'phone': row[5],
                'subject': row[6],
                'day1': row[7],
                'day1_time': row[8],
                'day2': row[9],
                'day2_time': row[10],
                'el': row[11],
                'pi': row[12],
                'v': row[13],
            }
        )

    return summary


def has_duplicate_names():
    """Check if there are any duplicate names in the active student list."""
    duplicates = get_duplicate_names()
    return len(duplicates) > 0