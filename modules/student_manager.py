#*****************************
#student_manager.py   ver 04--------------
#*****************************

import sqlite3, csv, os, json
from modules.database import DB_PATH
from modules import qr_generator

MAX_SUBJECTS = 3
MAX_SCHEDULE_DAYS = 6


def _photo_url(student_id, has_photo):
    return f"/students/photo/{student_id}" if has_photo else ''


def _coerce_blob(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, memoryview):
        return raw_value.tobytes()
    if isinstance(raw_value, bytes):
        return raw_value
    return None


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


def _classification_label(el=0, pi=0, v=0, ind=0, **_):
    """Return the primary classification label for a student."""
    if int(bool(el)):
        return "Assisted"
    if int(bool(pi)):
        return "Monitored"
    if int(bool(ind)):
        return "Independent"
    if int(bool(v)):
        return "Virtual"
    return "Monitored"


def _normalize_schedule_entries(schedule_json, *schedule_pairs):
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
        for day, time in schedule_pairs:
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
        (row[12], row[13]),
        (row[14], row[15]),
        (row[22] if len(row) > 22 else '', row[23] if len(row) > 23 else ''),
        (row[24] if len(row) > 24 else '', row[25] if len(row) > 25 else ''),
        (row[26] if len(row) > 26 else '', row[27] if len(row) > 27 else ''),
        (row[28] if len(row) > 28 else '', row[29] if len(row) > 29 else ''),
    )
    schedule_slots = [None] * MAX_SCHEDULE_DAYS
    for idx, entry in enumerate(schedule_entries[:MAX_SCHEDULE_DAYS]):
        schedule_slots[idx] = entry

    photo_blob = _coerce_blob(row[17] if len(row) > 17 else None)
    return {
        'id': row[0],
        'name': row[1],
        'subject': row[2],
        'email': row[3],
        'phone': row[4],
        'guardian': str(row[19] or '') if len(row) > 19 else '',
        'active': bool(row[5]),
        'book_loaned': bool(row[6]),
        'device_loaned': bool(row[7]),
        'el': bool(row[8]),
        'pi': bool(row[9]),
        'subjects': subjects,
        'subject_slots': subject_slots,
        'photo_url': _photo_url(row[0], bool(photo_blob)),
        'has_photo': bool(photo_blob),
        'classification': _classification_label(el=row[8], pi=row[9], v=row[16], ind=row[20] if len(row) > 20 else 0),
        'virtual': bool(row[16]),
        'schedule': schedule_entries,
        'schedule_slots': schedule_slots,
        'qr_filename': f"student_{row[0]}.png",
        'checkout_notify_enabled': bool(row[21]) if len(row) > 21 else True,
    }


def get_student_database_rows(active=1):
    """Get student rows tailored for the Student Database screen only."""
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        cols = [row[1] for row in c.execute("PRAGMA table_info(students)").fetchall()]
        has_checkout_notify = "checkout_notify_enabled" in cols

        has_book_loans_table = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='book_loans' LIMIT 1"
        ).fetchone() is not None
        has_material_loans_table = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='material_loans' LIMIT 1"
        ).fetchone() is not None

        checkout_notify_select = "checkout_notify_enabled" if has_checkout_notify else "1 AS checkout_notify_enabled"
        book_loaned_select = (
            "CASE WHEN EXISTS ("
            "SELECT 1 FROM book_loans bl "
            "WHERE bl.student_id = students.id AND bl.return_date IS NULL"
            ") THEN 1 ELSE COALESCE(students.book_loaned, 0) END"
            if has_book_loans_table
            else "COALESCE(students.book_loaned, 0)"
        )
        device_loaned_select = (
            "CASE WHEN EXISTS ("
            "SELECT 1 FROM material_loans ml "
            "WHERE ml.student_id = students.id AND ml.return_date IS NULL"
            ") THEN 1 ELSE COALESCE(students.device_loaned, 0) END"
            if has_material_loans_table
            else "COALESCE(students.device_loaned, 0)"
        )
        query = (
            "SELECT "
            f"id, name, subject, email, phone, active, {book_loaned_select} AS book_loaned, {device_loaned_select} AS device_loaned, "
            "el, pi, subjects_json, schedule_json, day1, day1_time, day2, day2_time, "
            "v, photo_blob, photo_mime, guardian, ind, "
            f"{checkout_notify_select} "
            ", day3, day3_time, day4, day4_time, day5, day5_time, day6, day6_time "
            "FROM students "
            "WHERE active = ? "
            "ORDER BY name"
        )

        c.execute(query, (active,))
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


def _normalize_csv_key(key):
    return str(key or '').strip().lower().replace(' ', '').replace('_', '').replace('-', '')


def _csv_get(row, *aliases, default=''):
    """Case/format-insensitive CSV field lookup."""
    if not isinstance(row, dict):
        return default
    wanted = {_normalize_csv_key(a) for a in aliases if a}
    for key, value in row.items():
        if _normalize_csv_key(key) in wanted:
            return value if value is not None else default
    return default


def _csv_bool(value):
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _csv_marked(value):
    """Return True for marker-style CSV values (e.g., x)."""
    token = str(value or '').strip().lower()
    return token in {'x', '1', 'true', 'yes', 'y', 'on', '✓', '✔'}


def _normalize_subject_name(raw_subject):
    token = str(raw_subject or '').strip()
    key = token.lower()

    # Legacy S# subject designations are mapped without hard-coded legacy labels.
    if key.startswith('s') and key[1:].isdigit():
        if key[1:] == '1':
            return 'Reading'
        if key[1:] == '2':
            return 'Math'

    mapping = {
        'm': 'Math',
        'math': 'Math',
        'mathematics': 'Math',
        'r': 'Reading',
        'reading': 'Reading',
        'read': 'Reading',
        'w': 'Writing',
        'writing': 'Writing',
        'write': 'Writing',
    }
    return mapping.get(key, token)


def _parse_subjects_from_csv(row):
    """Parse subjects from CSV values (new and legacy formats)."""
    subjects = []

    # Preferred format: M/R/W columns, marker value means selected.
    if _csv_marked(_csv_get(row, 'm', 'math', default='')):
        subjects.append('Math')
    if _csv_marked(_csv_get(row, 'r', 'reading', default='')):
        subjects.append('Reading')
    if _csv_marked(_csv_get(row, 'w', 'writing', default='')):
        subjects.append('Writing')

    raw_subjects = str(_csv_get(row, 'subjects', 'subjects_json', default='') or '').strip()
    if raw_subjects and not subjects:
        if '|' in raw_subjects:
            tokens = raw_subjects.split('|')
        elif ';' in raw_subjects:
            tokens = raw_subjects.split(';')
        else:
            tokens = raw_subjects.split(',')
        subjects.extend([t.strip() for t in tokens if t and t.strip()])

    for key in ('subject_1', 'subject_2', 'subject_3'):
        val = str(_csv_get(row, key, default='') or '').strip()
        if val and not subjects:
            subjects.append(val)

    legacy_subject = str(_csv_get(row, 'subject', default='') or '').strip()
    if legacy_subject and not subjects:
        subjects = [legacy_subject]

    deduped = []
    seen = set()
    for s in subjects:
        normalized_subject = _normalize_subject_name(s)
        k = normalized_subject.lower().strip()
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(normalized_subject)

    if not deduped:
        deduped = ['Math']

    return deduped[:MAX_SUBJECTS]


def _parse_classification_from_csv(row):
    """Parse classification into checkbox flags used by the edit form."""
    el = int(_csv_bool(_csv_get(row, 'el', 'assisted', default='0')))
    pi = int(_csv_bool(_csv_get(row, 'pi', 'monitored', default='0')))
    v = int(_csv_bool(_csv_get(row, 'v', 'virtual', default='0')))
    ind = int(_csv_bool(_csv_get(row, 'ind', 'independent', default='0')))

    raw = str(_csv_get(row, 'classification', default='') or '').strip().lower()
    if raw:
        if raw in ('assisted', 'a'):
            return 1, 0, 0, 0
        if raw in ('monitored', 'm'):
            return 0, 1, 0, 0
        if raw in ('independent', 'i'):
            return 0, 0, 0, 1
        if raw in ('virtual', 'v'):
            return 0, 0, 1, 0

    # Enforce single primary classification (same model as edit form)
    if el:
        return 1, 0, 0, 0
    if pi:
        return 0, 1, 0, 0
    if ind:
        return 0, 0, 0, 1
    if v:
        return 0, 0, 1, 0
    return 0, 1, 0, 0


def _normalize_schedule_entries_from_json(raw_schedule_json):
    entries = []
    seen = set()
    if not raw_schedule_json:
        return entries
    try:
        data = json.loads(raw_schedule_json)
    except (TypeError, ValueError):
        return entries
    if not isinstance(data, list):
        return entries
    for entry in data:
        if not isinstance(entry, dict):
            continue
        day = str(entry.get('day') or '').strip()
        time = str(entry.get('time') or '').strip()
        if not day or day in seen:
            continue
        seen.add(day)
        entries.append({'day': day, 'time': time})
        if len(entries) >= MAX_SCHEDULE_DAYS:
            break
    return entries


def _parse_schedule_from_csv(row):
    """Parse schedule in one of these formats:
    - schedule_json: JSON list of {day,time}
    - schedule: Monday@15:00|Wednesday@16:00 (also accepts `Day Time`)
    - legacy day1/day1_time/day2/day2_time columns
    """
    entries = _normalize_schedule_entries_from_json(_csv_get(row, 'schedule_json', default=''))

    raw_schedule = str(_csv_get(row, 'schedule', default='') or '').strip()
    if raw_schedule:
        seen = {e['day'] for e in entries}
        for chunk in raw_schedule.split('|'):
            token = str(chunk or '').strip()
            if not token:
                continue
            day = ''
            time = ''
            if '@' in token:
                day, time = token.split('@', 1)
            elif ':' in token and ' ' in token:
                day, time = token.rsplit(' ', 1)
            else:
                day = token
            day = str(day or '').strip()
            time = str(time or '').strip()
            if not day or day in seen:
                continue
            seen.add(day)
            entries.append({'day': day, 'time': time})
            if len(entries) >= MAX_SCHEDULE_DAYS:
                break

    if not entries:
        seen = set()
        for day_key, time_key in (
            ('day1', 'day1_time'),
            ('day2', 'day2_time'),
            ('day3', 'day3_time'),
            ('day4', 'day4_time'),
            ('day5', 'day5_time'),
            ('day6', 'day6_time'),
        ):
            day = str(_csv_get(row, day_key, default='') or '').strip()
            time = str(_csv_get(row, time_key, default='') or '').strip()
            if not day or day in seen:
                continue
            seen.add(day)
            entries.append({'day': day, 'time': time})

    entries = entries[:MAX_SCHEDULE_DAYS]
    slot_values = []
    for idx in range(MAX_SCHEDULE_DAYS):
        if idx < len(entries):
            slot_values.extend([entries[idx]['day'], entries[idx]['time']])
        else:
            slot_values.extend(['', ''])
    return (*slot_values, json.dumps(entries))


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
               SELECT s.id, s.name, s.subject, s.email, s.phone, COALESCE(s.guardian, '') AS guardian, '' AS legacy_contact, s.active, s.book_loaned, s.device_loaned,
                 s.el, s.pi, s.v, s.day1, s.day1_time, s.day2, s.day2_time, s.subjects_json, s.subject_minutes_json, s.total_study_minutes,
                  s.photo_blob,
                  COALESCE(s.photo_mime, '') AS photo_mime,
                                    COALESCE(s.ind, 0) AS ind,
                                    COALESCE(s.schedule_json, '') AS schedule_json,
                                    COALESCE(s.day3, '') AS day3,
                                    COALESCE(s.day3_time, '') AS day3_time,
                                    COALESCE(s.day4, '') AS day4,
                                    COALESCE(s.day4_time, '') AS day4_time,
                                    COALESCE(s.day5, '') AS day5,
                                    COALESCE(s.day5_time, '') AS day5_time,
                                    COALESCE(s.day6, '') AS day6,
                                    COALESCE(s.day6_time, '') AS day6_time
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
             SELECT id,name,subject,email,phone,'' AS legacy_contact,active,book_loaned,device_loaned,
                 el,pi,v,day1,day2,day1_time,day2_time,subjects_json,subject_minutes_json,total_study_minutes,
                 photo_blob,
                 COALESCE(photo_mime,'') AS photo_mime,
                 COALESCE(schedule_json,'') AS schedule_json,
                 COALESCE(guardian,'') AS guardian,
                 COALESCE(ind,0) AS ind,
                 COALESCE(checkout_notify_enabled,1) AS checkout_notify_enabled,
                 COALESCE(day3,'') AS day3,
                 COALESCE(day3_time,'') AS day3_time,
                 COALESCE(day4,'') AS day4,
                 COALESCE(day4_time,'') AS day4_time,
                 COALESCE(day5,'') AS day5,
                 COALESCE(day5_time,'') AS day5_time,
                 COALESCE(day6,'') AS day6,
                 COALESCE(day6_time,'') AS day6_time
            FROM students WHERE id=?
        """, (student_id,)).fetchone()
        return row


def get_student_static_profile(student_id):
    """Get a single student by ID as a dictionary."""
    row = get_student(student_id)
    if not row:
        return None
    schedule_entries = _normalize_schedule_entries(
        row[21] if len(row) > 21 else '',
        (row[12] if len(row) > 12 else '', row[14] if len(row) > 14 else ''),
        (row[13] if len(row) > 13 else '', row[15] if len(row) > 15 else ''),
        (row[25] if len(row) > 25 else '', row[26] if len(row) > 26 else ''),
        (row[27] if len(row) > 27 else '', row[28] if len(row) > 28 else ''),
        (row[29] if len(row) > 29 else '', row[30] if len(row) > 30 else ''),
        (row[31] if len(row) > 31 else '', row[32] if len(row) > 32 else ''),
    )
    return {
        'id': row[0],
        'name': row[1],
        'subject': row[2],
        'email': row[3],
        'phone': row[4],
        'active': row[6],
        'book_loaned': row[7],
        'device_loaned': row[8],
        'el': row[9],
        'pi': row[10],
        'v': row[11],
        'day1': row[12],
        'day2': row[13],
        'day1_time': row[14],
        'day2_time': row[15],
        'day3': row[25] if len(row) > 25 else '',
        'day3_time': row[26] if len(row) > 26 else '',
        'day4': row[27] if len(row) > 27 else '',
        'day4_time': row[28] if len(row) > 28 else '',
        'day5': row[29] if len(row) > 29 else '',
        'day5_time': row[30] if len(row) > 30 else '',
        'day6': row[31] if len(row) > 31 else '',
        'day6_time': row[32] if len(row) > 32 else '',
        'subjects': json.loads(row[16] or '[]') if len(row) > 16 else ([row[2]] if row[2] else []),
        'subject_minutes': json.loads(row[17] or '[]') if len(row) > 17 else ([30] if row[2] else []),
        'total_study_minutes': int(row[18] or 30) if len(row) > 18 else 30,
        'photo_blob': _coerce_blob(row[19] if len(row) > 19 else None),
        'photo_mime': str(row[20] or '') if len(row) > 20 else '',
        'guardian': str(row[22] or '') if len(row) > 22 else '',
        'photo_url': _photo_url(row[0], bool(_coerce_blob(row[19] if len(row) > 19 else None))),
        'checkout_notify_enabled': bool(row[24]) if len(row) > 24 else True,
        'schedule': schedule_entries,
    }


def get_student_photo(student_id):
    """Return the stored photo blob and metadata for a student."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT photo_blob, COALESCE(photo_mime, '') AS photo_mime
            FROM students
            WHERE id=?
            """,
            (student_id,),
        ).fetchone()
        if not row:
            return None
        blob = _coerce_blob(row['photo_blob'])
        return {
            'photo_blob': blob,
            'photo_mime': str(row['photo_mime'] or '') or 'image/png',
        }


def set_student_photo(student_id, photo_blob=None, photo_mime='', photo_filename='', legacy_photo=''):
    """Set or clear a student's photo bytes and metadata.

    Legacy args ``photo_filename`` and ``legacy_photo`` are retained for
    backward compatibility with older call sites but are ignored.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE students SET photo_blob=?, photo_mime=? WHERE id=?",
            (
                sqlite3.Binary(photo_blob) if photo_blob else None,
                photo_mime or '',
                student_id,
            ),
        )
        conn.commit()


def set_student_qr_code(student_id, qr_blob):
    """Store a student's QR code as BLOB in database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE students SET qr_code=? WHERE id=?",
            (sqlite3.Binary(qr_blob) if qr_blob else None, student_id),
        )
        conn.commit()

def set_checkout_notify_enabled(student_id, enabled):
    """Persist whether student should receive class checkout email notifications."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE students SET checkout_notify_enabled=? WHERE id=?",
            (1 if bool(enabled) else 0, student_id),
        )
        conn.commit()

def get_student_qr_code(student_id):
    """Retrieve a student's QR code blob from database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT qr_code FROM students WHERE id=?",
            (student_id,),
        ).fetchone()
        if row and row['qr_code']:
            return _coerce_blob(row['qr_code'])
    return None


def add_student(name, subject, email, phone, book_loaned=0, el=0, pi=0, v=0, ind=0, day1="", day2="", day1_time="", day2_time="", day3="", day3_time="", day4="", day4_time="", day5="", day5_time="", day6="", day6_time="", subjects=None, subject_minutes=None, schedule_json="", guardian=""):
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
            (name,subject,subjects_json,subject_minutes_json,total_study_minutes,email,phone,guardian,active,book_loaned,el,pi,v,ind,day1,day2,day1_time,day2_time,day3,day3_time,day4,day4_time,day5,day5_time,day6,day6_time,schedule_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                name,
                primary_subject,
                json.dumps(subjects_list),
                json.dumps(minutes_list),
                total_minutes,
                email,
                phone,
                str(guardian or '').strip(),
                1,
                int(bool(book_loaned)),
                int(bool(el)),
                int(bool(pi)),
                int(bool(v)),
                int(bool(ind)),
                day1,
                day2,
                day1_time,
                day2_time,
                day3,
                day3_time,
                day4,
                day4_time,
                day5,
                day5_time,
                day6,
                day6_time,
                schedule_json,
            ))
        student_id = c.lastrowid
        conn.commit()
    
    # Automatically generate QR code for the new student and store in DB only if not exists
    try:
        existing_qr = get_student_qr_code(student_id)
        if not existing_qr:
            qr_data = f"ID:{student_id}\nName:{name}"
            qr_blob = qr_generator.generate_qr_bytes(qr_data)
            set_student_qr_code(student_id, qr_blob)
    except Exception as e:
        print(f"Warning: Failed to generate QR code for student {student_id}: {e}")
    return student_id



def update_student(sid, name, email, phone, subject="", book_loaned=0, el=0, pi=0, v=0, ind=0, day1="", day2="", day1_time="", day2_time="", day3="", day3_time="", day4="", day4_time="", day5="", day5_time="", day6="", day6_time="", subjects=None, subject_minutes=None, schedule_json="", guardian=""):
    """Update an existing student's information with ownership check."""
    subjects_list, minutes_list, total_minutes = normalize_subject_entries(
        subjects if subjects is not None else [subject],
        subject_minutes if subject_minutes is not None else [30],
    )
    primary_subject = subjects_list[0]

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""UPDATE students SET name=?,subject=?,subjects_json=?,subject_minutes_json=?,total_study_minutes=?,email=?,phone=?,guardian=?,book_loaned=?,el=?,pi=?,v=?,ind=?,day1=?,day2=?,day1_time=?,day2_time=?,day3=?,day3_time=?,day4=?,day4_time=?,day5=?,day5_time=?,day6=?,day6_time=?,schedule_json=? WHERE id=?""",
                  (
                      name,
                      primary_subject,
                      json.dumps(subjects_list),
                      json.dumps(minutes_list),
                      total_minutes,
                      email,
                      phone,
                      str(guardian or '').strip(),
                      int(bool(book_loaned)),
                      int(bool(el)),
                      int(bool(pi)),
                      int(bool(v)),
                      int(bool(ind)),
                      day1,
                      day2,
                      day1_time,
                      day2_time,
                      day3,
                      day3_time,
                      day4,
                      day4_time,
                      day5,
                      day5_time,
                      day6,
                      day6_time,
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
            SELECT s.id, s.name, s.subject, s.email, s.phone, '' AS legacy_contact, s.active, s.book_loaned, s.device_loaned,
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
    
    with sqlite3.connect(DB_PATH) as conn, open(file_path,newline="",encoding="utf-8-sig") as f:
        reader=csv.DictReader(f)
        
        # Debug: Log column headers
        first_row = True
        for row in reader:
            if first_row:
                print(f"CSV Columns: {list(row.keys())}")
                first_row = False
            
            name = str(_csv_get(row, 'name', 'student_name', default='') or '').strip()
            if not name.strip(): continue
            
            email = str(_csv_get(row, 'email', default='') or '').strip()
            phone = str(_csv_get(row, 'phone', default='') or '').strip()
            guardian = str(_csv_get(row, 'guardian', 'guardian_name', 'parent', default='') or '').strip()

            subjects = _parse_subjects_from_csv(row)
            subject_minutes = [30] * len(subjects)
            total_study_minutes = sum(subject_minutes)
            primary_subject = subjects[0]

            el, pi, v, ind = _parse_classification_from_csv(row)
            
            # Check if student exists (owned by this user)
            student_record=conn.execute("SELECT id FROM students WHERE LOWER(TRIM(name))=LOWER(?)",(name.strip(),)).fetchone()
            
            if student_record:
                # UPDATE existing student - set all fields from CSV
                student_id = student_record[0]
                print(f"UPDATING student ID {student_id}: {name}")
                conn.execute(
                    """
                    UPDATE students
                    SET
                        name=?,
                        subject=?,
                        subjects_json=?,
                        subject_minutes_json=?,
                        total_study_minutes=?,
                        email=?,
                        phone=?,
                        guardian=?,
                        active=1,
                        el=?,
                        pi=?,
                        v=?,
                        ind=?
                    WHERE id=?
                    """,
                    (
                        name,
                        primary_subject,
                        json.dumps(subjects),
                        json.dumps(subject_minutes),
                        total_study_minutes,
                        email,
                        phone,
                        guardian,
                        el,
                        pi,
                        v,
                        ind,
                        student_id,
                    ),
                )
                updated+=1
            else:
                print(f"INSERTING new student: {name}")
                conn.execute(
                    """
                    INSERT INTO students(
                        name,
                        subject,
                        subjects_json,
                        subject_minutes_json,
                        total_study_minutes,
                        email,
                        phone,
                        guardian,
                        active,
                        el,
                        pi,
                        v,
                        ind
                    )
                    VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)
                    """,
                    (
                        name,
                        primary_subject,
                        json.dumps(subjects),
                        json.dumps(subject_minutes),
                        total_study_minutes,
                        email,
                        phone,
                        guardian,
                        el,
                        pi,
                        v,
                        ind,
                    ),
                )
                student_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                try:
                    qr_data = f"ID:{student_id}\nName:{name}"
                    qr_blob = qr_generator.generate_qr_bytes(qr_data)
                    conn.execute("UPDATE students SET qr_code=? WHERE id=?", (sqlite3.Binary(qr_blob), student_id))
                except Exception as qr_err:
                    print(f"Warning: Failed to generate QR for new student {student_id}: {qr_err}")
                added+=1
        
        conn.commit()
    return {"added": added, "updated": updated}

def export_csv(path):
    """Export active students in the same shape used by the student edit form CSV import."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT
                name,
                COALESCE(email, ''),
                COALESCE(phone, ''),
                COALESCE(guardian, ''),
                COALESCE(subjects_json, '[]'),
                COALESCE(subject, ''),
                el,
                pi,
                v,
                COALESCE(ind, 0)
            FROM students
            WHERE active = 1
            ORDER BY name
            """
        ).fetchall()

    headers = [
        "name",
        "email",
        "phone",
        "guardian",
        "M",
        "R",
        "W",
        "classification",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            name, email, phone, guardian = row[0], row[1], row[2], row[3]
            subjects_json, legacy_subject = row[4], row[5]
            el, pi, v, ind = int(bool(row[6])), int(bool(row[7])), int(bool(row[8])), int(bool(row[9]))

            try:
                subjects = [str(s).strip() for s in json.loads(subjects_json or '[]') if str(s or '').strip()]
            except (TypeError, ValueError):
                subjects = []
            if not subjects and legacy_subject:
                subjects = [str(legacy_subject).strip()]

            normalized_subjects = {_normalize_subject_name(s).lower() for s in subjects}
            m_flag = 'x' if 'math' in normalized_subjects else ''
            r_flag = 'x' if 'reading' in normalized_subjects else ''
            w_flag = 'x' if 'writing' in normalized_subjects else ''

            classification = _classification_label(el=el, pi=pi, v=v, ind=ind)

            writer.writerow([
                name,
                email,
                phone,
                guardian,
                m_flag,
                r_flag,
                w_flag,
                classification,
            ])


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