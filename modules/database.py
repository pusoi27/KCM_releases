#*****************************
#database.py   ver 05--
#*****************************

import sqlite3, os, sys, shutil, threading, time, atexit, socket
from datetime import datetime, timezone
import json

DATA_DIR = os.getenv("DATA_DIR", "data")
LOCAL_FALLBACK_DB_PATH = os.path.join("data", "Stdytime.db")

# ---------------------------------------------------------------------------
# Config file (db_config.json next to app.py).
# Supported keys:
#   db_path              – local machine path for all session reads/writes
#   gdrive_sync_path     – Google Drive path used only for background sync
#   sync_interval_minutes – how often local is pushed to GDrive (0 = off)
# ---------------------------------------------------------------------------
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_CONFIG_FILE = os.path.join(_APP_ROOT, "db_config.json")


def _read_db_config() -> dict:
    """Return the full config dict from db_config.json."""
    try:
        with open(_DB_CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[startup] WARNING: could not read {_DB_CONFIG_FILE}: {exc}", file=sys.stderr)
        return {}


def _read_db_config_path() -> str | None:
    """Return db_path from db_config.json, or None if absent/invalid."""
    path = _read_db_config().get("db_path", "").strip()
    return path if path else None


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
    # Priority: db_config.json db_path > DB_PATH env var > DATA_DIR default
    config_path = _read_db_config_path()
    preferred = config_path or os.getenv("DB_PATH", os.path.join(DATA_DIR, "Stdytime.db"))
    is_usable, reason = _can_use_db_parent(preferred)
    if is_usable:
        print(f"[startup] Local DB path: {preferred}")
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


# ====================================================================
# Google Drive sync
# ====================================================================

def _sqlite_backup(src_path: str, dst_path: str):
    """
    Safe online SQLite backup using the built-in backup API.
    Handles WAL mode correctly; dst is written atomically via a .syncing tmp file.
    """
    dst_dir = os.path.dirname(dst_path) or "."
    os.makedirs(dst_dir, exist_ok=True)
    tmp = dst_path + ".syncing"
    src_conn = sqlite3.connect(src_path)
    dst_conn = sqlite3.connect(tmp)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    os.replace(tmp, dst_path)


def _db_summary(db_path: str) -> str:
    """
    Return a one-line summary of the DB state: record counts for key tables
    and the file size.  Never raises — returns a fallback string on error.
    """
    try:
        size_kb = os.path.getsize(db_path) / 1024
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        counts = {}
        for table in ("students", "sessions", "staff", "books", "assistant_sessions"):
            try:
                counts[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                counts[table] = "?"
        conn.close()
        return (
            f"students={counts['students']}  sessions={counts['sessions']}  "
            f"staff={counts['staff']}  books={counts['books']}  "
            f"assistant_sessions={counts['assistant_sessions']}  "
            f"file={size_kb:.1f} KB"
        )
    except Exception as exc:
        return f"(summary unavailable: {exc})"


def sync_from_gdrive(local_path: str, gdrive_path: str) -> bool:
    """
    Pull GDrive → local on startup if the GDrive copy is newer.
    Returns True if a pull was performed.
    """
    if not gdrive_path or not os.path.exists(gdrive_path):
        return False
    try:
        gdrive_mtime = os.path.getmtime(gdrive_path)
        local_mtime = os.path.getmtime(local_path) if os.path.exists(local_path) else 0
        if gdrive_mtime > local_mtime + 10:   # 10 s tolerance avoids noise
            print(f"[sync] Pulling DB from Google Drive (GDrive is newer): {gdrive_path}")
            _sqlite_backup(gdrive_path, local_path)
            print("[sync] Pull complete.")
            return True
        else:
            print("[sync] Local DB is up-to-date; no pull needed.")
            return False
    except Exception as exc:
        print(f"[sync] WARNING: pull from GDrive failed: {exc}", file=sys.stderr)
        return False


def sync_to_gdrive(local_path: str, gdrive_path: str, retries: int = 0, retry_delay: int = 5, silent: bool = False) -> bool:
    """
    Push local → GDrive.  Returns True on success.
    Safe to call from a background thread.
    retries: number of additional attempts if the file is locked (PermissionError).
    retry_delay: seconds to wait between attempts.
    silent: if True, suppresses all console output (used by background thread).
    """
    if not gdrive_path:
        return False
    if not os.path.exists(local_path):
        if not silent:
            print("[sync] WARNING: local DB does not exist yet; skipping push.", file=sys.stderr)
        return False
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            _sqlite_backup(local_path, gdrive_path)
            if not silent:
                summary = _db_summary(local_path)
                print(
                    f"[sync] Pushed DB to Google Drive: {gdrive_path}\n"
                    f"[sync] Snapshot → {summary}"
                )
            return True
        except PermissionError as exc:
            if attempt <= retries:
                if not silent:
                    print(
                        f"[sync] GDrive file is locked (attempt {attempt}/{attempts}): {exc}. "
                        f"Retrying in {retry_delay}s...",
                        file=sys.stderr,
                    )
                time.sleep(retry_delay)
            else:
                if not silent:
                    print(f"[sync] WARNING: push to GDrive failed after {attempts} attempt(s): {exc}", file=sys.stderr)
                return False
        except Exception as exc:
            if not silent:
                print(f"[sync] WARNING: push to GDrive failed: {exc}", file=sys.stderr)
            return False
    return False


def sync_to_gdrive_now() -> bool:
    """Public entry point for on-demand push (callable from routes)."""
    return sync_to_gdrive(DB_PATH, GDRIVE_SYNC_PATH)


# ====================================================================
# GDrive exclusive-use lock
# ====================================================================

class GDriveLockError(RuntimeError):
    """Raised when another machine holds the GDrive DB lock."""


def _lock_path(gdrive_sync_path: str) -> str:
    """Return the .lock file path next to the GDrive DB."""
    base = os.path.splitext(gdrive_sync_path)[0]
    return base + ".lock"


def acquire_gdrive_lock(gdrive_sync_path: str, timeout_minutes: int = 60) -> bool:
    """
    Write a lock file to GDrive claiming this machine owns the DB.
    Raises GDriveLockError if another live machine holds the lock.
    Returns False if gdrive_sync_path is not set (no-op).
    """
    if not gdrive_sync_path:
        return False

    lock_file = _lock_path(gdrive_sync_path)
    gdrive_dir = os.path.dirname(gdrive_sync_path)

    # Check for an existing lock
    if os.path.exists(lock_file):
        try:
            with open(lock_file, encoding="utf-8") as fh:
                info = json.load(fh)
            locked_at = datetime.fromisoformat(info.get("locked_at", ""))
            age_minutes = (datetime.now(timezone.utc) - locked_at).total_seconds() / 60
            if age_minutes < timeout_minutes:
                holder = info.get("machine", "unknown")
                pid = info.get("pid", "?")
                raise GDriveLockError(
                    f"DB is locked by machine '{holder}' (PID {pid}), "
                    f"locked {age_minutes:.0f} min ago. "
                    f"Close StdyTime on that machine first, or wait "
                    f"{timeout_minutes - age_minutes:.0f} min for the lock to expire."
                )
            else:
                print(
                    f"[lock] Stale lock detected ({age_minutes:.0f} min old, "
                    f"threshold {timeout_minutes} min). Taking over.",
                    file=sys.stderr,
                )
        except GDriveLockError:
            raise
        except Exception as exc:
            print(f"[lock] Could not read existing lock file: {exc}. Overwriting.", file=sys.stderr)

    # Write our lock
    try:
        os.makedirs(gdrive_dir, exist_ok=True)
        lock_data = {
            "machine": socket.gethostname(),
            "pid": os.getpid(),
            "locked_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(lock_file, "w", encoding="utf-8") as fh:
            json.dump(lock_data, fh, indent=2)
        print(f"[lock] GDrive lock acquired on {lock_data['machine']} (PID {lock_data['pid']})")
        return True
    except Exception as exc:
        print(f"[lock] WARNING: could not write lock file: {exc}", file=sys.stderr)
        return False


def release_gdrive_lock(gdrive_sync_path: str) -> bool:
    """
    Remove the lock file only if it belongs to this process.
    Returns True if removed, False otherwise.
    """
    if not gdrive_sync_path:
        return False
    lock_file = _lock_path(gdrive_sync_path)
    if not os.path.exists(lock_file):
        return False
    try:
        with open(lock_file, encoding="utf-8") as fh:
            info = json.load(fh)
        if info.get("machine") == socket.gethostname() and info.get("pid") == os.getpid():
            os.remove(lock_file)
            print("[lock] GDrive lock released.")
            return True
        else:
            print("[lock] Lock belongs to a different process; not removing.", file=sys.stderr)
            return False
    except Exception as exc:
        print(f"[lock] WARNING: could not release lock file: {exc}", file=sys.stderr)
        return False


def _start_background_sync(local_path: str, gdrive_path: str, interval_minutes: int):
    """Daemon thread that pushes local → GDrive every interval_minutes."""
    if interval_minutes <= 0 or not gdrive_path:
        return

    def _loop():
        while True:
            time.sleep(interval_minutes * 60)
            sync_to_gdrive(local_path, gdrive_path, silent=True)

    t = threading.Thread(target=_loop, daemon=True, name="gdrive-sync")
    t.start()
    print(f"[sync] Background sync thread started (every {interval_minutes} min → {gdrive_path})")


# ====================================================================
# Module-level initialisation
# ====================================================================

_cfg = _read_db_config()
GDRIVE_SYNC_PATH: str | None = _cfg.get("gdrive_sync_path", "").strip() or None
_SYNC_INTERVAL = int(_cfg.get("sync_interval_minutes", 5))
_STARTUP_PULL_ENABLED = str(_cfg.get("startup_pull_from_gdrive", "false")).strip().lower() == "true"

DB_PATH = _resolve_db_path()

# On startup: optional pull from GDrive if it is newer than local copy.
# Default is disabled so local DB remains source-of-truth for runtime speed.
if _STARTUP_PULL_ENABLED:
    sync_from_gdrive(DB_PATH, GDRIVE_SYNC_PATH)
else:
    print("[sync] Startup pull disabled; local DB is source of truth.")

# Background thread: push local → GDrive periodically
_start_background_sync(DB_PATH, GDRIVE_SYNC_PATH, _SYNC_INTERVAL)

# On clean exit: release lock then do one final push
def _sync_on_exit():
    """
    Called by atexit. Releases the GDrive lock then pushes the local DB to
    Google Drive.  If GDrive has a file lock (Drive client is mid-sync) it
    retries up to 12 times (1 minute total) and prints a visible warning
    so the user knows NOT to shut down the machine yet.
    """
    release_gdrive_lock(GDRIVE_SYNC_PATH)

    if not GDRIVE_SYNC_PATH or not os.path.exists(DB_PATH):
        return

    _MAX_ATTEMPTS = 12
    _RETRY_DELAY  = 5   # seconds between retries

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            _sqlite_backup(DB_PATH, GDRIVE_SYNC_PATH)
            summary = _db_summary(DB_PATH)
            print(
                f"[sync] Final exit push to Google Drive complete: {GDRIVE_SYNC_PATH}\n"
                f"[sync] Snapshot → {summary}"
            )
            return
        except PermissionError:
            remaining = (_MAX_ATTEMPTS - attempt) * _RETRY_DELAY
            print(
                f"\n*** StdyTime: Google Drive file is busy (attempt {attempt}/{_MAX_ATTEMPTS}). "
                f"Please wait ~{remaining}s before shutting down this machine. ***",
                file=sys.stderr,
            )
            time.sleep(_RETRY_DELAY)
        except Exception as exc:
            print(f"[sync] WARNING: final exit push failed: {exc}", file=sys.stderr)
            return

    print(
        "\n*** StdyTime WARNING: Could not push DB to Google Drive after exit. "
        "Your latest data is safe locally but NOT yet synced to Google Drive. "
        "Restart the app when Google Drive is available to sync. ***",
        file=sys.stderr,
    )


atexit.register(_sync_on_exit)

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