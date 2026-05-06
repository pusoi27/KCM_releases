#*****************************
#database.py   ver 04--
#*****************************

import sqlite3, os, sys
from datetime import datetime
import json

DATA_DIR = os.getenv("DATA_DIR", "data")
LOCAL_FALLBACK_DB_PATH = os.path.join("data", "Stdytime.db")

# ---------------------------------------------------------------------------
# Local machine config (db_config.json next to app.py) overrides env vars.
# Each machine can point to its own Google Drive path without touching code.
# Format: { "db_path": "C:/Users/you/Google Drive/My Drive/Stdytime/Stdytime.db" }
# ---------------------------------------------------------------------------
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_CONFIG_FILE = os.path.join(_APP_ROOT, "db_config.json")

def _read_db_config_path() -> str | None:
    """Return the db_path from db_config.json, or None if absent/invalid."""
    try:
        with open(_DB_CONFIG_FILE, encoding="utf-8") as fh:
            cfg = json.load(fh)
        path = cfg.get("db_path", "").strip()
        return path if path else None
    except FileNotFoundError:
        return None
    except Exception as exc:
        print(f"[startup] WARNING: could not read {_DB_CONFIG_FILE}: {exc}", file=sys.stderr)
        return None


def _can_use_db_parent(path):
    """Return (is_usable, reason)."""
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as exc:
        return False, str(exc)

    probe_path = os.path.join(parent, ".stdytime_db_write_probe")
    try:
        with open(probe_path, "w", encoding="utf-8") as probe:
            probe.write("ok")
        os.remove(probe_path)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _resolve_db_path():
    # Priority: db_config.json > DB_PATH env var > DATA_DIR default
    config_path = _read_db_config_path()
    preferred = config_path or os.getenv("DB_PATH", os.path.join(DATA_DIR, "Stdytime.db"))
    is_usable, reason = _can_use_db_parent(preferred)
    if is_usable:
        if config_path:
            print(f"[startup] Using Google Drive / network DB from db_config.json: {preferred}")
        return preferred

    normalized = preferred.replace("\\", "/")
    if normalized.startswith("/var/data"):
        fallback_usable, fallback_reason = _can_use_db_parent(LOCAL_FALLBACK_DB_PATH)
        if fallback_usable:
            print(
                f"[startup] WARNING: DB_PATH '{preferred}' is unavailable ({reason}). "
                f"Falling back to '{LOCAL_FALLBACK_DB_PATH}'. "
                "Attach a Render persistent disk mounted at /var/data for durable storage.",
                file=sys.stderr
            )
            return LOCAL_FALLBACK_DB_PATH
        raise RuntimeError(
            f"DB_PATH '{preferred}' unavailable ({reason}); fallback '{LOCAL_FALLBACK_DB_PATH}' also unavailable ({fallback_reason})"
        )

    raise RuntimeError(f"DB_PATH '{preferred}' is unavailable: {reason}")


DB_PATH = _resolve_db_path()

def init_db():
    db_parent = os.path.dirname(DB_PATH)
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # WAL mode allows concurrent reads during Google Drive sync without corruption
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")

    # Students
    c.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            subject TEXT,
            subjects_json TEXT DEFAULT '[]',
            subject_minutes_json TEXT DEFAULT '[]',
            total_study_minutes INTEGER DEFAULT 30,
            level TEXT,
            book_loaned INTEGER DEFAULT 0,
            paper_ws INTEGER DEFAULT 0,
            email TEXT,
            phone TEXT,
            active INTEGER DEFAULT 1,
            math_goal TEXT DEFAULT '',
            math_ws_per_week INTEGER DEFAULT 0,
            reading_goal TEXT DEFAULT '',
            reading_ws_per_week INTEGER DEFAULT 0,
            el INTEGER DEFAULT 0,
            pi INTEGER DEFAULT 0,
            v INTEGER DEFAULT 0,
            day1 TEXT DEFAULT '',
            day1_time TEXT DEFAULT '',
            day2 TEXT DEFAULT '',
            day2_time TEXT DEFAULT ''
        )
    """)

    # Staff
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            role TEXT,
            email TEXT,
            phone TEXT
        )
    """)

    # Books
    c.execute("""
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            author TEXT,
            isbn TEXT,
            isbn13 TEXT,
            publisher TEXT,
            available INTEGER DEFAULT 1,
            reading_level TEXT,
            copies INTEGER DEFAULT 1,
            borrower_id INTEGER
        )
    """)

    # Sessions (attendance)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            start_time TEXT,
            end_time TEXT,
            duration INTEGER,
            FOREIGN KEY(student_id) REFERENCES students(id)
        )
    """)

    # Assistant sessions (staff hours)
    c.execute("""
        CREATE TABLE IF NOT EXISTS assistant_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id INTEGER,
            start_time TEXT,
            end_time TEXT,
            duration INTEGER,
            FOREIGN KEY(assistant_id) REFERENCES staff(id)
        )
    """)

    # Instructor Profile
    c.execute("""
        CREATE TABLE IF NOT EXISTS instructor_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            center_location TEXT,
            center_address TEXT,
            center_time_zone TEXT,
            center_hours TEXT,
            monday_start TEXT, monday_end TEXT,
            tuesday_start TEXT, tuesday_end TEXT,
            wednesday_start TEXT, wednesday_end TEXT,
            thursday_start TEXT, thursday_end TEXT,
            friday_start TEXT, friday_end TEXT,
            saturday_start TEXT, saturday_end TEXT,
            sunday_start TEXT, sunday_end TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Assistant Schedule (day-based scheduling for center operations)
    c.execute("""
        CREATE TABLE IF NOT EXISTS assistant_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_id INTEGER NOT NULL,
            scheduled_date TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(assistant_id) REFERENCES staff(id),
            UNIQUE(assistant_id, scheduled_date)
        )
    """)

    # Explicit center-closed calendar dates (e.g., holiday closures)
    c.execute("""
        CREATE TABLE IF NOT EXISTS center_closed_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            closed_date TEXT NOT NULL,
            reason TEXT DEFAULT 'Holiday / Center Closed',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(closed_date)
        )
    """)

    # Local machine license state
    c.execute("""
        CREATE TABLE IF NOT EXISTS app_license (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            license_key TEXT,
            licensee TEXT,
            email TEXT,
            issued_at TEXT,
            expires_at TEXT,
            machine_fingerprint TEXT,
            metadata_json TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    # Sample data
    if not c.execute("SELECT COUNT(*) FROM students").fetchone()[0]:
        demo = [
            ("Alice Johnson", "S1", "6A", 0, 1, "alice@demo.com", "111-222", 1),
            ("Bob Smith", "S2", "5A", 1, 0, "bob@demo.com", "222-333", 1)
        ]
        c.executemany("""INSERT INTO students
            (name, subject, level, book_loaned, paper_ws, email, phone, active)
            VALUES (?,?,?,?,?,?,?,?)""", demo)

    if not c.execute("SELECT COUNT(*) FROM staff").fetchone()[0]:
        c.execute("INSERT INTO staff (name,role,email,phone) VALUES (?,?,?,?)",
                  ("John Doe","Admin","admin@demo.com","777-888"))

    if not c.execute("SELECT COUNT(*) FROM books").fetchone()[0]:
        c.execute("INSERT INTO books (title,author,isbn,available,reading_level)"
                  " VALUES (?,?,?,?,?)",
                  ("Mathematics Basics","KumoPress","111222333",1,"5A"))
    conn.commit(); conn.close()

    # Ensure additional columns exist on students table (migration for additional fields)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(students)")
        cols = [r[1] for r in cur.fetchall()]
        if "math_goal" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN math_goal TEXT")
        if "math_worksheets_per_week" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN math_worksheets_per_week INTEGER DEFAULT 0")
        if "math_ws_per_week" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN math_ws_per_week INTEGER DEFAULT 0")
        if "reading_goal" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN reading_goal TEXT")
        if "reading_worksheets_per_week" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN reading_worksheets_per_week INTEGER DEFAULT 0")
        if "reading_ws_per_week" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN reading_ws_per_week INTEGER DEFAULT 0")
        # Add new fields: EL, PI, V checkboxes and Day 1, Day 2 schedule fields
        if "el" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN el INTEGER DEFAULT 0")
        if "pi" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN pi INTEGER DEFAULT 0")
        if "v" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN v INTEGER DEFAULT 0")
        if "day1" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day1 TEXT")
        if "day2" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day2 TEXT")
        if "day1_time" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day1_time TEXT")
        if "day2_time" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day2_time TEXT")
        if "subjects_json" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN subjects_json TEXT DEFAULT '[]'")
        if "subject_minutes_json" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN subject_minutes_json TEXT DEFAULT '[]'")
        if "total_study_minutes" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN total_study_minutes INTEGER DEFAULT 30")
        if "photo" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN photo TEXT DEFAULT ''")
        if "schedule_json" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN schedule_json TEXT DEFAULT ''")
        conn.commit()

    # Ensure required columns exist on staff table; drop orphaned columns
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(staff)")
        cols = [r[1] for r in cur.fetchall()]
        if "whatsapp" in cols:
            cur.execute("ALTER TABLE staff DROP COLUMN whatsapp")
        conn.commit()

    # Ensure new book columns exist (migration for book inventory management)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(books)")
        cols = [r[1] for r in cur.fetchall()]
        
        if "copies" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN copies INTEGER DEFAULT 1")
        if "isbn13" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN isbn13 TEXT")
        if "publisher" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN publisher TEXT")
        if "borrower_id" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN borrower_id INTEGER REFERENCES students(id)")
        
        conn.commit()

    # Ensure app_license table has all expected columns
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(app_license)")
        cols = [r[1] for r in cur.fetchall()]
        if not cols:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_license (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    license_key TEXT,
                    licensee TEXT,
                    email TEXT,
                    issued_at TEXT,
                    expires_at TEXT,
                    machine_fingerprint TEXT,
                    metadata_json TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute("PRAGMA table_info(app_license)")
            cols = [r[1] for r in cur.fetchall()]
        if "licensee" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN licensee TEXT")
        if "email" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN email TEXT")
        if "issued_at" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN issued_at TEXT")
        if "expires_at" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN expires_at TEXT")
        if "machine_fingerprint" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN machine_fingerprint TEXT")
        if "metadata_json" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN metadata_json TEXT DEFAULT '{}' ")
        if "updated_at" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN updated_at TEXT DEFAULT CURRENT_TIMESTAMP")
        # LemonSqueezy integration columns
        if "ls_instance_id" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN ls_instance_id TEXT DEFAULT ''")
        if "ls_status" not in cols:
            cur.execute("ALTER TABLE app_license ADD COLUMN ls_status TEXT DEFAULT ''")
        conn.commit()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(sessions)")
        cols = [r[1] for r in cur.fetchall()]
        cur.execute("PRAGMA table_info(assistant_sessions)")
        cols = [r[1] for r in cur.fetchall()]
        conn.commit()

    # Ensure assistant_schedule table exists
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(assistant_schedule)")
        cols = [r[1] for r in cur.fetchall()]
        conn.commit()

    # Ensure center_closed_dates table and columns exist
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS center_closed_dates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                closed_date TEXT NOT NULL,
                reason TEXT DEFAULT 'Holiday / Center Closed',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(closed_date)
            )
            """
        )
        cur.execute("PRAGMA table_info(center_closed_dates)")
        cols = [r[1] for r in cur.fetchall()]
        if "reason" not in cols and cols:
            cur.execute("ALTER TABLE center_closed_dates ADD COLUMN reason TEXT DEFAULT 'Holiday / Center Closed'")
        conn.commit()

    # Ensure instructor_profile has center_hours column (migration for center operating hours)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(instructor_profile)")
        cols = [r[1] for r in cur.fetchall()]
        
        if "center_hours" not in cols:
            cur.execute("ALTER TABLE instructor_profile ADD COLUMN center_hours TEXT")
        
        if "center_address" not in cols:
            cur.execute("ALTER TABLE instructor_profile ADD COLUMN center_address TEXT")

        if "center_time_zone" not in cols:
            cur.execute("ALTER TABLE instructor_profile ADD COLUMN center_time_zone TEXT")
        
        # Add weekly hours columns (start and end time for each day of week)
        days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        for day in days:
            start_col = f"{day}_start"
            end_col = f"{day}_end"
            if start_col not in cols:
                cur.execute(f"ALTER TABLE instructor_profile ADD COLUMN {start_col} TEXT")
            if end_col not in cols:
                cur.execute(f"ALTER TABLE instructor_profile ADD COLUMN {end_col} TEXT")
        
        conn.commit()

    def ensure_template():
        os.makedirs("templates", exist_ok=True)
        tpl_path = os.path.join("templates", "student_template.csv")
        if not os.path.exists(tpl_path):
            with open(tpl_path, "w", encoding="utf-8") as f:
                f.write("name,subject,level,email,phone\n")
                f.write("Example Student,S1,6A,example@example.com,123456789\n")


# ====================================================================
# Database connection helper
# ====================================================================

def get_db_connection():
    """Open and return a database connection to Stdytime.db"""
    return sqlite3.connect(DB_PATH)


# ====================================================================
# Helper functions for Levels tables (loaded from Excel)
# ====================================================================

def get_expected_level(grade: str, subject: str, month: str):
    """
    Get expected level for a grade/subject/month combination
    from levels_by_grade table.
    
    Args:
        grade: Grade level (e.g., 'Grade 1', 'PK2', 'K')
        subject: 'reading' or 'math'
        month: Month name (e.g., 'Sept', 'Dec')
    
    Returns:
        Dictionary with 'level' and 'page_index' or None if not found
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT level, page_index 
        FROM levels_by_grade 
        WHERE grade = ? AND subject = ? AND month = ?
    """, (grade, subject, month))
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {'level': row[0], 'page_index': row[1]}
    return None


def get_page_index(level: str, subject: str):
    """
    Get page_index for a specific level and subject.
    
    Args:
        level: Level string (e.g., 'F80', 'AI120')
        subject: 'reading' or 'math'
    
    Returns:
        page_index (int) or None if not found
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT page_index 
        FROM levels_by_grade 
        WHERE level = ? AND subject = ?
        LIMIT 1
    """, (level, subject))
    
    row = cursor.fetchone()
    conn.close()
    
    return row[0] if row else None


def get_worksheets_per_day_db(level_begin: str, subject: str):
    """
    Get worksheets per day for a level from levels_index table.
    
    Args:
        level_begin: Beginning level (e.g., '7A', 'AI')
        subject: 'reading' or 'math'
    
    Returns:
        worksheets_per_day (int) or None if not found
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT "worksheets per day"
        FROM levels_index_index_table 
        WHERE subject = ? AND "level begin" = ?
    """, (subject, level_begin))
    
    row = cursor.fetchone()
    conn.close()
    
    return row[0] if row else None


def get_level_range(subject: str, level_begin: str):
    """
    Get full level range information from levels_index table.
    
    Args:
        subject: 'reading' or 'math'
        level_begin: Beginning level (e.g., '7A', 'F')
    
    Returns:
        Dictionary with level range info or None
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            "level begin", 
            "low index", 
            "level end", 
            "high index", 
            "worksheets per day"
        FROM levels_index_index_table 
        WHERE subject = ? AND "level begin" = ?
    """, (subject, level_begin))
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            'level_begin': row[0],
            'low_index': row[1],
            'level_end': row[2],
            'high_index': row[3],
            'worksheets_per_day': row[4]
        }
    return None


def query_levels_by_grade(filters=None):
    """
    Query levels_by_grade table with optional filters.
    
    Args:
        filters: Dict with optional keys: 'grade', 'subject', 'month'
    
    Returns:
        List of dictionaries with query results
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = "SELECT subject, grade, month, level, page_index FROM levels_by_grade"
    params = []
    
    if filters:
        conditions = []
        if 'grade' in filters:
            conditions.append("grade = ?")
            params.append(filters['grade'])
        if 'subject' in filters:
            conditions.append("subject = ?")
            params.append(filters['subject'])
        if 'month' in filters:
            conditions.append("month = ?")
            params.append(filters['month'])
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
    
    query += " ORDER BY subject, grade, month"
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for row in rows:
        results.append({
            'subject': row[0],
            'grade': row[1],
            'month': row[2],
            'level': row[3],
            'page_index': row[4]
        })
    
    return results