#*****************************
#database.py   ver 05--
#*****************************

import sqlite3, os, sys, shutil, threading, time, atexit, socket, mimetypes
from datetime import datetime, timezone
import json
import tempfile

try:
    import ctypes
except Exception:  # pragma: no cover - platform/runtime defensive guard
    ctypes = None

DATA_DIR = os.getenv("DATA_DIR", "data")
LOCAL_FALLBACK_DB_PATH = os.path.join("data", "Stdytime.db")


def _default_local_db_path_for_runtime() -> str:
    """Return the safest default DB path for the current runtime context.

    - Frozen/installed Windows app: use a per-user LOCALAPPDATA location.
    - Source/dev runs: keep the historical relative ./data path.
    """
    if getattr(sys, "frozen", False):
        local_appdata = os.getenv("LOCALAPPDATA", "").strip()
        if local_appdata:
            return os.path.join(local_appdata, "StdyTime", "Stdytime.db")

        # Defensive fallback when LOCALAPPDATA is unavailable
        return os.path.join(os.path.expanduser("~"), "AppData", "Local", "StdyTime", "Stdytime.db")

    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(source_root, DATA_DIR, "Stdytime.db")

def _get_persistent_config_dir() -> str:
    """Return a stable config directory for the current runtime."""
    if getattr(sys, "frozen", False):
        local_appdata = os.getenv("LOCALAPPDATA", "").strip()
        if local_appdata:
            return os.path.join(local_appdata, "StdyTime")
        return os.path.join(os.path.expanduser("~"), "AppData", "Local", "StdyTime")

    # Source/dev runs keep config next to project files.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_DIR = _get_persistent_config_dir()
_DB_CONFIG_FILE = os.path.join(_CONFIG_DIR, "db_config.json")
_LEGACY_DB_CONFIG_FILE = os.path.join(_APP_ROOT, "db_config.json")
_GDRIVE_DISCOVERY_ATTEMPTED = False
_FIXED_SYNC_INTERVAL_MINUTES = 9
_APP_VERSION_META_KEY = "app_version"
_LAST_SYNC_ERROR = ""
_SHUTDOWN_SYNC_COMPLETED = False
_SHUTDOWN_WAIT_POPUP_SHOWN = False
_SHUTDOWN_SYNC_LOCK = threading.Lock()
_CLOUD_SYNC_RUNTIME_READY = False


def _set_last_sync_error(message: str) -> None:
    global _LAST_SYNC_ERROR
    _LAST_SYNC_ERROR = str(message or "").strip()


def get_last_sync_error() -> str:
    """Return the most recent sync failure reason for UI feedback."""
    return _LAST_SYNC_ERROR


def _ensure_app_metadata_table(conn: sqlite3.Connection) -> None:
    """Ensure app metadata table exists for cross-machine compatibility fields."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _parse_version_tuple(version: str) -> tuple[int, ...] | None:
    raw = str(version or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def _compare_versions(left: str, right: str) -> int:
    """Return -1 when left<right, 0 when equal, 1 when left>right."""
    l_tuple = _parse_version_tuple(left)
    r_tuple = _parse_version_tuple(right)

    if l_tuple is not None and r_tuple is not None:
        max_len = max(len(l_tuple), len(r_tuple))
        l_pad = l_tuple + (0,) * (max_len - len(l_tuple))
        r_pad = r_tuple + (0,) * (max_len - len(r_tuple))
        if l_pad < r_pad:
            return -1
        if l_pad > r_pad:
            return 1
        return 0

    left_norm = str(left or "").strip().lower()
    right_norm = str(right or "").strip().lower()
    if left_norm < right_norm:
        return -1
    if left_norm > right_norm:
        return 1
    return 0


def get_recorded_app_version(db_path: str | None = None) -> str:
    """Return recorded DB app version metadata (latest known writer version)."""
    target_db = db_path or DB_PATH
    try:
        with sqlite3.connect(target_db) as conn:
            _ensure_app_metadata_table(conn)
            row = conn.execute(
                "SELECT COALESCE(meta_value, '') FROM app_metadata WHERE meta_key = ? LIMIT 1",
                (_APP_VERSION_META_KEY,),
            ).fetchone()
            return str((row[0] if row else "") or "").strip()
    except Exception as exc:
        print(f"[version] WARNING: failed reading recorded DB app version: {exc}", file=sys.stderr)
        return ""


def record_app_version(app_version: str, db_path: str | None = None) -> str:
    """Persist app version in DB metadata, never downgrading an existing newer value."""
    current = str(app_version or "").strip()
    if not current:
        return get_recorded_app_version(db_path=db_path)

    target_db = db_path or DB_PATH
    try:
        with sqlite3.connect(target_db) as conn:
            _ensure_app_metadata_table(conn)
            row = conn.execute(
                "SELECT COALESCE(meta_value, '') FROM app_metadata WHERE meta_key = ? LIMIT 1",
                (_APP_VERSION_META_KEY,),
            ).fetchone()
            existing = str((row[0] if row else "") or "").strip()

            # Never overwrite a newer version with an older binary's version.
            if existing and _compare_versions(current, existing) < 0:
                return existing

            conn.execute(
                """
                INSERT INTO app_metadata (meta_key, meta_value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(meta_key) DO UPDATE SET
                    meta_value=excluded.meta_value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (_APP_VERSION_META_KEY, current),
            )
            conn.commit()
            return current
    except Exception as exc:
        print(f"[version] WARNING: failed recording DB app version metadata: {exc}", file=sys.stderr)
        return get_recorded_app_version(db_path=db_path)


def get_version_compatibility_warning(app_version: str) -> dict:
    """Return compatibility warning payload when binary is older than DB backup version."""
    current = str(app_version or "").strip()
    recorded = get_recorded_app_version()
    if not current or not recorded:
        return {
            "show": False,
            "app_version": current,
            "backup_version": recorded,
            "message": "",
        }

    is_older_than_backup = _compare_versions(current, recorded) < 0
    if not is_older_than_backup:
        return {
            "show": False,
            "app_version": current,
            "backup_version": recorded,
            "message": "",
        }

    return {
        "show": True,
        "app_version": current,
        "backup_version": recorded,
        "message": (
            f"Update required: this machine is running v{current}, "
            f"but the shared backup database was updated by v{recorded}. "
            "Please update this application to the latest release."
        ),
    }


def _read_db_config() -> dict:
    """Return the full config dict from db_config.json."""
    try:
        # One-time migration path for packaged installs that used to read from app root.
        if (
            getattr(sys, "frozen", False)
            and not os.path.exists(_DB_CONFIG_FILE)
            and os.path.exists(_LEGACY_DB_CONFIG_FILE)
        ):
            try:
                os.makedirs(_CONFIG_DIR, exist_ok=True)
                shutil.copy2(_LEGACY_DB_CONFIG_FILE, _DB_CONFIG_FILE)
                print(f"[startup] Migrated db config to persistent path: {_DB_CONFIG_FILE}")
            except Exception as exc:
                print(f"[startup] WARNING: failed to migrate legacy db config: {exc}", file=sys.stderr)

        with open(_DB_CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[startup] WARNING: could not read {_DB_CONFIG_FILE}: {exc}", file=sys.stderr)
        return {}


def _write_db_config(cfg: dict) -> None:
    """Persist the config dict to db_config.json."""
    os.makedirs(os.path.dirname(_DB_CONFIG_FILE), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="db_config_", suffix=".tmp", dir=os.path.dirname(_DB_CONFIG_FILE))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, _DB_CONFIG_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _to_absolute_path(path: str) -> str:
    """Return an absolute normalized path (relative paths resolve from app root)."""
    candidate = (path or "").strip()
    if not candidate:
        return ""
    if os.path.isabs(candidate):
        return os.path.abspath(candidate)
    return os.path.abspath(os.path.join(_APP_ROOT, candidate))


def _read_db_config_path() -> str | None:
    """Return db_path from db_config.json, or None if absent/invalid."""
    cfg = _read_db_config()
    raw_path = str(cfg.get("db_path", "") or "").strip()
    if not raw_path:
        return None

    absolute = _to_absolute_path(raw_path)
    if absolute and absolute != raw_path:
        try:
            cfg["db_path"] = absolute.replace("\\", "/")
            _write_db_config(cfg)
        except Exception as exc:
            print(f"[startup] WARNING: could not normalize db_path to absolute path: {exc}", file=sys.stderr)

    return absolute if absolute else None


def _normalize_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/")


def _is_google_drive_location(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    if not normalized:
        return False
    return (
        normalized.startswith("g:/")
        or "my drive" in normalized
        or "google drive" in normalized
    )


def _is_onedrive_location(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    if not normalized:
        return False
    return "onedrive" in normalized


def _is_supported_cloud_location(path: str, provider: str) -> bool:
    provider = (provider or "").strip().lower()
    if provider == "onedrive":
        return _is_onedrive_location(path)
    return _is_onedrive_location(path)


def _resolve_cloud_provider_and_path(cfg: dict) -> tuple[str, str]:
    provider = str(cfg.get("cloud_provider", "") or "").strip().lower()
    onedrive_sync_path = _normalize_path(str(cfg.get("onedrive_sync_path", "") or ""))

    if onedrive_sync_path:
        return "onedrive", onedrive_sync_path
    return "onedrive", ""


def _cloud_label(sync_path: str) -> str:
    return "OneDrive" if _is_onedrive_location(sync_path) else "Google Drive"


def _resolve_gdrive_sync_target(gdrive_sync_path: str) -> str:
    """Return the actual DB file path used for Google Drive sync.

    The setup screen may store either a folder path (preferred) or an explicit
    database file path for backwards compatibility. Folder inputs resolve to a
    Stdytime.db file inside that folder.
    """
    path = _normalize_path(gdrive_sync_path)
    if not path:
        return ""

    last_segment = os.path.basename(path.rstrip("/"))
    if not os.path.splitext(last_segment)[1]:
        return os.path.join(path, "Stdytime.db").replace("\\", "/")
    return path


def _cloud_path_exists(sync_path: str) -> bool:
    """Return True when the configured cloud folder/DB path exists."""
    raw = _normalize_path(sync_path)
    if not raw:
        return False
    target = _resolve_gdrive_sync_target(raw)
    return os.path.isdir(raw) or os.path.exists(target)


def _extract_valid_gdrive_path_from_config_file(config_file: str) -> tuple[float, str] | None:
    """Return (mtime, gdrive_sync_path) when a config file contains a usable path."""
    try:
        with open(config_file, encoding="utf-8") as fh:
            cfg = json.load(fh)
        candidate = _normalize_path(str(cfg.get("gdrive_sync_path", "") or ""))
        if not candidate:
            return None
        if not _is_google_drive_location(candidate):
            return None
        if not _cloud_path_exists(candidate):
            return None
        return (os.path.getmtime(config_file), candidate)
    except Exception:
        return None


def _discover_existing_gdrive_sync_path() -> str:
    """Find an already-configured Google Drive path from previous local installs."""
    candidates: list[tuple[float, str]] = []

    # 1) Same install folder (if current config exists but was read before rewrite)
    if os.path.exists(_DB_CONFIG_FILE):
        found = _extract_valid_gdrive_path_from_config_file(_DB_CONFIG_FILE)
        if found:
            candidates.append(found)

    # 2) Previous per-version app folders under LOCALAPPDATA
    local_appdata = (os.getenv("LOCALAPPDATA", "") or "").strip()
    if local_appdata and os.path.isdir(local_appdata):
        try:
            for entry in os.listdir(local_appdata):
                lowered = entry.lower()
                if not lowered.startswith("stdytime"):
                    continue
                folder = os.path.join(local_appdata, entry)
                if not os.path.isdir(folder):
                    continue
                config_file = os.path.join(folder, "db_config.json")
                if not os.path.isfile(config_file):
                    continue
                found = _extract_valid_gdrive_path_from_config_file(config_file)
                if found:
                    candidates.append(found)
        except Exception:
            pass

    if not candidates:
        return ""

    # Prefer most recently modified config as the canonical machine setting.
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _hydrate_missing_gdrive_sync_path(cfg: dict, db_path: str) -> str:
    """Auto-fill gdrive_sync_path for upgrades, then persist to current config."""
    global _GDRIVE_DISCOVERY_ATTEMPTED

    if _GDRIVE_DISCOVERY_ATTEMPTED:
        return ""
    _GDRIVE_DISCOVERY_ATTEMPTED = True

    discovered = _discover_existing_gdrive_sync_path()
    if not discovered:
        return ""

    cfg.setdefault("_comment", "db_path = local machine path (fast, all session reads/writes go here).")
    cfg.setdefault("_comment2", "gdrive_sync_path = Google Drive folder path used only for background sync; Stdytime.db is created there automatically.")
    cfg.setdefault("_comment3", "sync_interval_minutes = fixed system-managed value (9 minutes).")
    cfg["db_path"] = _normalize_path(db_path or "") or _default_local_db_path_for_runtime().replace("\\", "/")
    cfg["gdrive_sync_path"] = discovered
    cfg["sync_interval_minutes"] = _FIXED_SYNC_INTERVAL_MINUTES

    try:
        _write_db_config(cfg)
        print(f"[startup] Reused existing Google Drive backup path from this machine: {discovered}")
    except Exception as exc:
        print(f"[startup] WARNING: could not persist discovered Google Drive path: {exc}", file=sys.stderr)

    return discovered


def get_db_config_status() -> dict:
    """Return readiness status for local DB path + cloud backup sync path setup."""
    cfg = _read_db_config()
    db_path = str(cfg.get("db_path", "") or "").strip()
    if not db_path:
        # Local DB path is auto-managed; default it when not explicitly set.
        db_path = _default_local_db_path_for_runtime().replace("\\", "/")
    else:
        db_path = _to_absolute_path(db_path).replace("\\", "/")
    gdrive_sync_path = ""
    onedrive_sync_path = _normalize_path(str(cfg.get("onedrive_sync_path", "") or ""))
    cloud_provider, cloud_sync_path = _resolve_cloud_provider_and_path(cfg)

    issues: list[str] = []
    warnings: list[str] = []

    usable, reason = _can_use_db_parent(db_path)
    if not usable:
        issues.append(f"Local database path is not writable: {reason}")

    if not cloud_sync_path:
        issues.append(
            "OneDrive backup folder path is required. Example: C:/Users/YourName/OneDrive/StdyTime."
        )
    else:
        if not _is_supported_cloud_location(cloud_sync_path, cloud_provider):
            issues.append(
                "OneDrive folder path must point to your OneDrive folder, for example: C:/Users/YourName/OneDrive/StdyTime."
            )
        elif not _cloud_path_exists(cloud_sync_path):
            issues.append(
                "Configured cloud backup path does not exist yet. Create the folder first, then save again."
            )

    return {
        "is_ready": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "config": {
            "db_path": db_path,
            "cloud_provider": cloud_provider,
            "cloud_sync_path": cloud_sync_path,
            "gdrive_sync_path": gdrive_sync_path,
            "onedrive_sync_path": onedrive_sync_path,
            "sync_interval_minutes": _FIXED_SYNC_INTERVAL_MINUTES,
            "startup_pull_from_gdrive": bool(cfg.get("startup_pull_from_gdrive", False)),
        },
        "example": {
            "db_path": "C:/Users/YourName/AppData/Local/StdyTime/Stdytime.db",
            "onedrive_sync_path": "C:/Users/YourName/OneDrive/StdyTime",
        },
    }


def save_db_config_paths(
    *,
    db_path: str | None,
    gdrive_sync_path: str,
    onedrive_sync_path: str = "",
    cloud_provider: str = "onedrive",
) -> dict:
    """Persist db_path and cloud sync path settings to db_config.json and return updated status."""
    db_path = _normalize_path(db_path or "")
    gdrive_sync_path = ""
    onedrive_sync_path = _normalize_path(onedrive_sync_path or "")
    cloud_provider = "onedrive"

    cfg = _read_db_config()
    existing_db_path = str(cfg.get("db_path", "") or "").strip().replace("\\", "/")
    if not db_path:
        db_path = existing_db_path or _default_local_db_path_for_runtime().replace("\\", "/")

    db_path = _to_absolute_path(db_path).replace("\\", "/")

    cfg["_comment"] = "db_path = local machine path (fast, all session reads/writes go here)."
    cfg["_comment2"] = "cloud_provider = onedrive (Windows OneDrive backup destination)."
    cfg["_comment3"] = "sync_interval_minutes = fixed system-managed value (9 minutes)."
    cfg["_comment4"] = "onedrive_sync_path = folder path used only for background sync; Stdytime.db is created there automatically."

    cfg["db_path"] = db_path
    cfg["gdrive_sync_path"] = ""
    cfg["onedrive_sync_path"] = onedrive_sync_path
    cfg["cloud_provider"] = "onedrive"
    cfg["sync_interval_minutes"] = _FIXED_SYNC_INTERVAL_MINUTES

    _write_db_config(cfg)

    return get_db_config_status()


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


def _safe_remove_file(path: str, *, context: str = "temp file", silent: bool = True) -> bool:
    """Best-effort file deletion helper.

    Returns True when a file was removed, False otherwise.
    """
    target = str(path or "").strip()
    if not target:
        return False
    try:
        if os.path.isfile(target):
            os.remove(target)
            return True
    except Exception as exc:
        if not silent:
            print(f"[cleanup] WARNING: could not remove {context} '{target}': {exc}", file=sys.stderr)
    return False


def _resolve_db_path():
    # Priority: db_config.json db_path > DB_PATH env var > DATA_DIR default
    config_path = _read_db_config_path()
    env_db_path = os.getenv("DB_PATH", "").strip()
    preferred = config_path or env_db_path or _default_local_db_path_for_runtime()
    preferred = _to_absolute_path(preferred)
    is_usable, reason = _can_use_db_parent(preferred)
    if is_usable:
        print(f"[startup] Local DB path: {preferred}")
        return preferred

    # If the chosen path is relative/non-writable in an installed build,
    # transparently fall back to a per-user writable location.
    fallback_local = _default_local_db_path_for_runtime()
    should_try_local_fallback = (
        preferred != fallback_local
        and (not config_path and not env_db_path or not os.path.isabs(preferred))
    )
    if should_try_local_fallback:
        fallback_usable, fallback_reason = _can_use_db_parent(fallback_local)
        if fallback_usable:
            print(
                f"[startup] WARNING: DB_PATH '{preferred}' is unavailable ({reason}). "
                f"Falling back to '{fallback_local}'.",
                file=sys.stderr,
            )
            return fallback_local
        print(
            f"[startup] WARNING: DB_PATH '{preferred}' is unavailable ({reason}) and "
            f"fallback '{fallback_local}' is also unavailable ({fallback_reason}).",
            file=sys.stderr,
        )

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

    # If a previous interrupted run left a sync temp file behind, clear it first.
    _safe_remove_file(tmp, context="stale sync temp", silent=True)

    src_conn = sqlite3.connect(src_path)
    dst_conn = sqlite3.connect(tmp)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()

    try:
        os.replace(tmp, dst_path)
    finally:
        # Ensure the temporary sync artifact does not remain on disk,
        # including replacement failure or interruption scenarios.
        _safe_remove_file(tmp, context="sync temp", silent=True)


def _sqlite_restore_live(src_path: str, dst_path: str, retries: int = 3, retry_delay: float = 1.5) -> None:
    """Restore src SQLite DB into a live destination DB path.

    Unlike file replacement, this writes pages through SQLite's backup API
    directly into the destination database, which avoids Windows file-replace
    access-denied errors when the app process already has the DB file open.
    """
    attempts = max(1, int(retries))
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with sqlite3.connect(src_path) as src_conn, sqlite3.connect(dst_path, timeout=30) as dst_conn:
                dst_conn.execute("PRAGMA busy_timeout=30000")
                src_conn.backup(dst_conn)
                dst_conn.commit()
            return
        except sqlite3.OperationalError as exc:
            # Typical transient case while other requests are writing.
            last_exc = exc
            if attempt < attempts:
                time.sleep(retry_delay)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            raise

    if last_exc:
        raise last_exc


def _db_summary(db_path: str) -> str:
    """
    Return a one-line summary of the DB state: record counts for key tables
    and the file size.  Never raises — returns a fallback string on error.
    """
    try:
        size_kb = os.path.getsize(db_path) / 1024
        counts = {}
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            for table in ("students", "sessions", "staff", "books", "assistant_sessions"):
                try:
                    counts[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception:
                    counts[table] = "?"
        return (
            f"students={counts['students']}  sessions={counts['sessions']}  "
            f"staff={counts['staff']}  books={counts['books']}  "
            f"assistant_sessions={counts['assistant_sessions']}  "
            f"file={size_kb:.1f} KB"
        )
    except Exception as exc:
        return f"(summary unavailable: {exc})"


def _student_photos_dir() -> str:
    return os.path.join(_APP_ROOT, "static", "img", "students")


def _guess_photo_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename or "")
    return mime or "image/png"


def _migrate_student_photos_to_blob(db_path: str) -> None:
    """Backfill the new BLOB photo columns from the legacy filename column."""
    photos_dir = _student_photos_dir()
    if not os.path.isdir(photos_dir):
        return

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cols = [r[1] for r in cur.execute("PRAGMA table_info(students)").fetchall()]
        has_photo = "photo" in cols
        has_photo_filename = "photo_filename" in cols
        has_photo_blob = "photo_blob" in cols
        has_photo_mime = "photo_mime" in cols

        if not has_photo_blob:
            return
        if not has_photo and not has_photo_filename:
            return

        legacy_photo_expr = "COALESCE(photo, '')" if has_photo else "''"
        legacy_filename_expr = "COALESCE(photo_filename, '')" if has_photo_filename else "''"
        photo_mime_expr = "COALESCE(photo_mime, '')" if has_photo_mime else "''"

        rows = cur.execute(
            f"""
            SELECT id, {legacy_photo_expr} AS legacy_photo, {legacy_filename_expr} AS legacy_filename, {photo_mime_expr} AS photo_mime
            FROM students
            WHERE ({legacy_photo_expr} <> '' OR {legacy_filename_expr} <> '')
              AND (photo_blob IS NULL OR length(photo_blob) = 0)
            """
        ).fetchall()

        if not rows:
            return

        for student_id, legacy_photo, legacy_filename, photo_mime in rows:
            legacy_name = str(legacy_photo or legacy_filename or '').strip()
            if not legacy_name:
                continue

            photo_path = os.path.join(photos_dir, legacy_name)
            if not os.path.isfile(photo_path):
                continue

            try:
                with open(photo_path, 'rb') as fh:
                    blob = fh.read()
                if not blob:
                    continue
                cur.execute(
                    """
                    UPDATE students
                    SET photo_blob=?, photo_mime=?
                    WHERE id=?
                    """,
                    (
                        sqlite3.Binary(blob),
                        photo_mime or _guess_photo_mime(legacy_name),
                        student_id,
                    ),
                )
            except Exception as exc:
                print(f"[startup] WARNING: failed to migrate photo for student {student_id}: {exc}", file=sys.stderr)

        conn.commit()


_LEGACY_STUDENT_COLUMNS_TO_REMOVE = (
    "level",
    "photo",
    "photo_filename",
    "math_goal",
    "math_ws_per_week",
    "math_worksheets_per_week",
    "reading_goal",
    "reading_ws_per_week",
    "reading_worksheets_per_week",
)


def _remove_legacy_student_columns(db_path: str) -> None:
    """Drop legacy columns from students table, rebuilding table if needed."""
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cols = [r[1] for r in cur.execute("PRAGMA table_info(students)").fetchall()]
        to_drop = [col for col in _LEGACY_STUDENT_COLUMNS_TO_REMOVE if col in cols]
        if not to_drop:
            return

        try:
            for col in to_drop:
                cur.execute(f'ALTER TABLE students DROP COLUMN "{col}"')
            conn.commit()
            print(f"[startup] Removed legacy student goal columns: {', '.join(to_drop)}")
            return
        except Exception as exc:
            print(
                f"[startup] Direct DROP COLUMN for legacy goal fields failed ({exc}); rebuilding students table.",
                file=sys.stderr,
            )
            conn.rollback()

        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("DROP TABLE IF EXISTS students_new_no_goals")
        cur.execute(
            """
            CREATE TABLE students_new_no_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                subject TEXT,
                subjects_json TEXT DEFAULT '[]',
                subject_minutes_json TEXT DEFAULT '[]',
                total_study_minutes INTEGER DEFAULT 30,
                book_loaned INTEGER DEFAULT 0,
                email TEXT,
                phone TEXT,
                guardian TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                el INTEGER DEFAULT 0,
                pi INTEGER DEFAULT 0,
                v INTEGER DEFAULT 0,
                day1 TEXT DEFAULT '',
                day1_time TEXT DEFAULT '',
                day2 TEXT DEFAULT '',
                day2_time TEXT DEFAULT '',
                day3 TEXT DEFAULT '',
                day3_time TEXT DEFAULT '',
                day4 TEXT DEFAULT '',
                day4_time TEXT DEFAULT '',
                day5 TEXT DEFAULT '',
                day5_time TEXT DEFAULT '',
                day6 TEXT DEFAULT '',
                day6_time TEXT DEFAULT '',
                checkout_notify_enabled INTEGER DEFAULT 1,
                photo_blob BLOB,
                photo_mime TEXT DEFAULT '',
                schedule_json TEXT DEFAULT '',
                qr_code BLOB,
                device_loaned INTEGER DEFAULT 0,
                ind INTEGER DEFAULT 0
            )
            """
        )

        src_cols = set(cols)

        def _src(col_name: str, default_sql: str) -> str:
            if col_name in src_cols:
                return f'"{col_name}"'
            return f'{default_sql} AS "{col_name}"'

        insert_cols = [
            "id", "name", "subject", "subjects_json", "subject_minutes_json", "total_study_minutes",
            "book_loaned", "email", "phone", "guardian", "active", "el", "pi", "v",
            "day1", "day1_time", "day2", "day2_time", "day3", "day3_time", "day4", "day4_time",
            "day5", "day5_time", "day6", "day6_time", "checkout_notify_enabled", "photo_blob",
            "photo_mime", "schedule_json", "qr_code", "device_loaned", "ind",
        ]

        select_exprs = [
            _src("id", "NULL"),
            _src("name", "''"),
            _src("subject", "''"),
            _src("subjects_json", "'[]'"),
            _src("subject_minutes_json", "'[]'"),
            _src("total_study_minutes", "30"),
            _src("book_loaned", "0"),
            _src("email", "''"),
            _src("phone", "''"),
            _src("guardian", "''"),
            _src("active", "1"),
            _src("el", "0"),
            _src("pi", "0"),
            _src("v", "0"),
            _src("day1", "''"),
            _src("day1_time", "''"),
            _src("day2", "''"),
            _src("day2_time", "''"),
            _src("day3", "''"),
            _src("day3_time", "''"),
            _src("day4", "''"),
            _src("day4_time", "''"),
            _src("day5", "''"),
            _src("day5_time", "''"),
            _src("day6", "''"),
            _src("day6_time", "''"),
            _src("checkout_notify_enabled", "1"),
            _src("photo_blob", "NULL"),
            _src("photo_mime", "''"),
            _src("schedule_json", "''"),
            _src("qr_code", "NULL"),
            _src("device_loaned", "0"),
            _src("ind", "0"),
        ]

        cur.execute(
            f"""
            INSERT INTO students_new_no_goals ({', '.join(insert_cols)})
            SELECT {', '.join(select_exprs)}
            FROM students
            """
        )
        cur.execute("DROP TABLE students")
        cur.execute("ALTER TABLE students_new_no_goals RENAME TO students")
        cur.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        print(f"[startup] Rebuilt students table without legacy goal columns: {', '.join(to_drop)}")


def sync_from_gdrive(local_path: str, gdrive_path: str, force: bool = False) -> bool:
    """
    Pull GDrive → local.
    - force=False: pull only when GDrive mtime is newer than local.
    - force=True: always pull when a GDrive DB exists.
    Returns True if a pull was performed.
    """
    gdrive_path = _resolve_gdrive_sync_target(gdrive_path)
    if not gdrive_path:
        _set_last_sync_error("Cloud backup path is not configured.")
        return False
    if not os.path.exists(gdrive_path):
        _set_last_sync_error(f"Cloud backup database was not found at: {gdrive_path}")
        return False
    lock_acquired = False
    try:
        lock_acquired = acquire_gdrive_lock(
            gdrive_path,
            timeout_minutes=60,
            wait_seconds=0,
            poll_seconds=3,
            lease_seconds=300,
        )
        if not lock_acquired:
            _set_last_sync_error("Cloud backup lease is not available.")
            return False

        cloud_name = _cloud_label(gdrive_path)
        gdrive_mtime = os.path.getmtime(gdrive_path)
        local_mtime = os.path.getmtime(local_path) if os.path.exists(local_path) else 0
        should_pull = force or (gdrive_mtime > local_mtime + 10)  # 10 s tolerance avoids noise
        if should_pull:
            reason = "forced startup override" if force else "cloud backup is newer"
            print(f"[sync] Pulling DB from {cloud_name} ({reason}): {gdrive_path}")
            _sqlite_restore_live(gdrive_path, local_path)
            print("[sync] Pull complete.")
            _set_last_sync_error("")
            return True
        else:
            print("[sync] Local DB is up-to-date; no pull needed.")
            _set_last_sync_error("No pull needed because local database is already up to date.")
            return False
    except Exception as exc:
        print(f"[sync] WARNING: pull from GDrive failed: {exc}", file=sys.stderr)
        _set_last_sync_error(str(exc))
        return False
    finally:
        if lock_acquired:
            release_gdrive_lock(gdrive_path)


def sync_to_gdrive(local_path: str, gdrive_path: str, retries: int = 0, retry_delay: int = 5, silent: bool = False) -> bool:
    """
    Push local → GDrive.  Returns True on success.
    Safe to call from a background thread.
    retries: number of additional attempts if the file is locked (PermissionError).
    retry_delay: seconds to wait between attempts.
    silent: if True, suppresses all console output (used by background thread).
    """
    gdrive_path = _resolve_gdrive_sync_target(gdrive_path)
    if not gdrive_path:
        _set_last_sync_error("Cloud backup path is not configured.")
        return False
    if not os.path.exists(local_path):
        if not silent:
            print("[sync] WARNING: local DB does not exist yet; skipping push.", file=sys.stderr)
        _set_last_sync_error(f"Local database was not found at: {local_path}")
        return False
    cloud_name = _cloud_label(gdrive_path)
    attempts = retries + 1
    lock_acquired = False
    for attempt in range(1, attempts + 1):
        try:
            if not lock_acquired:
                lock_acquired = acquire_gdrive_lock(
                    gdrive_path,
                    timeout_minutes=60,
                    wait_seconds=0,
                    poll_seconds=3,
                    lease_seconds=300,
                )
                if not lock_acquired:
                    _set_last_sync_error("Cloud backup lease is not available.")
                    return False

            _sqlite_backup(local_path, gdrive_path)
            if not silent:
                summary = _db_summary(local_path)
                print(
                    f"[sync] Pushed DB to {cloud_name}: {gdrive_path}\n"
                    f"[sync] Snapshot -> {summary}"
                )
            _set_last_sync_error("")
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
                _set_last_sync_error(str(exc))
                return False
        except Exception as exc:
            if not silent:
                print(f"[sync] WARNING: push to GDrive failed: {exc}", file=sys.stderr)
            _set_last_sync_error(str(exc))
            return False
        finally:
            if lock_acquired:
                release_gdrive_lock(gdrive_path)
                lock_acquired = False
    return False


def sync_to_gdrive_now() -> bool:
    """Public entry point for on-demand push (callable from routes)."""
    return sync_to_gdrive(DB_PATH, GDRIVE_SYNC_PATH)


def sync_from_gdrive_now(force: bool = True) -> bool:
    """Public entry point for on-demand pull (callable from routes)."""
    return sync_from_gdrive(DB_PATH, GDRIVE_SYNC_PATH, force=force)


# ====================================================================
# GDrive exclusive-use lock
# ====================================================================

class GDriveLockError(RuntimeError):
    """Raised when another machine holds the GDrive DB lock."""


def _lock_path(gdrive_sync_path: str) -> str:
    """Return the .lock file path next to the GDrive DB."""
    gdrive_sync_path = _resolve_gdrive_sync_target(gdrive_sync_path)
    base = os.path.splitext(gdrive_sync_path)[0]
    return base + ".lock"


def _lock_owner_payload(*, lease_seconds: int) -> dict:
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "machine": socket.gethostname(),
        "pid": os.getpid(),
        "locked_at": now_iso,
        "heartbeat_at": now_iso,
        "lease_seconds": int(max(15, lease_seconds)),
    }


def _read_lock_info(lock_file: str) -> dict:
    try:
        with open(lock_file, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_lock_age_seconds(info: dict) -> float:
    heartbeat_raw = str((info or {}).get("heartbeat_at") or (info or {}).get("locked_at") or "").strip()
    if not heartbeat_raw:
        return float("inf")
    try:
        stamped = datetime.fromisoformat(heartbeat_raw)
        return max(0.0, (datetime.now(timezone.utc) - stamped).total_seconds())
    except Exception:
        return float("inf")


def acquire_gdrive_lock(
    gdrive_sync_path: str,
    timeout_minutes: int = 60,
    *,
    wait_seconds: int = 0,
    poll_seconds: int = 3,
    lease_seconds: int = 90,
) -> bool:
    """
    Write a short-lived lock file for a single cloud sync event.
    Raises GDriveLockError if another live machine holds the lock.
    Returns False if gdrive_sync_path is not set (no-op).
    """
    gdrive_sync_path = _resolve_gdrive_sync_target(gdrive_sync_path)
    if not gdrive_sync_path:
        return False

    lock_file = _lock_path(gdrive_sync_path)
    gdrive_dir = os.path.dirname(gdrive_sync_path)
    os.makedirs(gdrive_dir, exist_ok=True)

    # Backward-compat: timeout_minutes acts as optional stale-timeout cap.
    stale_timeout_seconds = int(max(30, timeout_minutes * 60))
    lease_seconds = int(max(15, lease_seconds))
    poll_seconds = int(max(1, poll_seconds))
    deadline = time.time() + max(0, int(wait_seconds))

    while True:
        lock_data = _lock_owner_payload(lease_seconds=lease_seconds)
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(lock_data, fh, indent=2)
            print(f"[lock] Cloud lock acquired on {lock_data['machine']} (PID {lock_data['pid']})")
            return True
        except FileExistsError:
            info = _read_lock_info(lock_file)
            holder = str(info.get("machine") or "unknown")
            pid = info.get("pid", "?")
            age_seconds = _parse_lock_age_seconds(info)
            holder_lease = int(max(lease_seconds, int(info.get("lease_seconds") or lease_seconds)))
            stale_threshold = max(30, holder_lease * 2)
            if timeout_minutes > 0:
                stale_threshold = min(stale_threshold, stale_timeout_seconds)

            # Same process already owns lock.
            if holder == socket.gethostname() and int(pid or -1) == os.getpid():
                return True

            # Stale lock takeover.
            if age_seconds >= stale_threshold:
                try:
                    os.remove(lock_file)
                    print(
                        f"[lock] Stale cloud lock detected ({age_seconds:.0f}s old, "
                        f"threshold {stale_threshold}s). Taking over.",
                        file=sys.stderr,
                    )
                    continue
                except Exception as exc:
                    print(f"[lock] Failed to remove stale lock: {exc}", file=sys.stderr)

            if time.time() < deadline:
                remaining = int(max(0, deadline - time.time()))
                print(
                    f"[lock] Cloud DB in use by '{holder}' (PID {pid}). "
                    f"Waiting... ({remaining}s left)",
                    file=sys.stderr,
                )
                time.sleep(poll_seconds)
                continue

            raise GDriveLockError(
                f"DB is in use by machine '{holder}' (PID {pid}). "
                f"Try again after that machine exits, or retry when lock expires."
            )
        except GDriveLockError:
            raise
        except Exception as exc:
            print(f"[lock] WARNING: could not acquire cloud lock: {exc}", file=sys.stderr)
            return False


def release_gdrive_lock(gdrive_sync_path: str) -> bool:
    """
    Remove the lock file only if it belongs to this process.
    Returns True if removed, False otherwise.
    """
    gdrive_sync_path = _resolve_gdrive_sync_target(gdrive_sync_path)
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
    print(f"[sync] Background sync thread started (every {interval_minutes} min -> {gdrive_path})")


def flush_cloud_backup_on_shutdown(
    *,
    status_after_seconds: int = 8,
    retry_delay_seconds: int = 3,
) -> bool:
    """Block shutdown until local DB has been pushed to cloud backup.

    - Returns True when sync is confirmed.
    - Returns True immediately when cloud backup is not configured.
    - Displays a temporary user-facing wait message when sync takes longer than
      ``status_after_seconds``.
    """
    sync_target = _resolve_gdrive_sync_target(GDRIVE_SYNC_PATH or "")
    if not sync_target:
        return True

    delay = max(1, int(retry_delay_seconds))
    warn_after = max(1, int(status_after_seconds))
    start_ts = time.monotonic()
    wait_notice_shown = False

    def _show_temporary_wait_popup(timeout_seconds: int = 8) -> None:
        """Show a temporary non-blocking Windows popup while shutdown waits for sync."""
        if os.name != 'nt' or ctypes is None:
            return

        message = (
            "Stdytime is finishing cloud backup sync before closing.\n\n"
            "Please wait and do not force-close the app."
        )
        title = "Stdytime - Finishing Backup"
        timeout_ms = max(3000, int(timeout_seconds * 1000))

        def _popup_worker() -> None:
            try:
                user32 = ctypes.windll.user32
                # MessageBoxTimeoutW auto-closes after timeout (milliseconds).
                msg_timeout = getattr(user32, "MessageBoxTimeoutW", None)
                if msg_timeout:
                    msg_timeout.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_wchar_p,
                        ctypes.c_wchar_p,
                        ctypes.c_uint,
                        ctypes.c_ushort,
                        ctypes.c_uint,
                    ]
                    msg_timeout.restype = ctypes.c_int
                    MB_OK = 0x00000000
                    MB_ICONINFORMATION = 0x00000040
                    MB_SETFOREGROUND = 0x00010000
                    msg_timeout(
                        None,
                        message,
                        title,
                        MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND,
                        0,
                        timeout_ms,
                    )
                    return

                # Fallback when MessageBoxTimeoutW is unavailable.
                user32.MessageBoxW(None, message, title, 0x00000040)
            except Exception:
                pass

        threading.Thread(target=_popup_worker, daemon=True, name="shutdown-sync-popup").start()

    while True:
        if sync_to_gdrive(DB_PATH, sync_target, retries=0, retry_delay=delay, silent=True):
            if wait_notice_shown:
                print("[shutdown] Cloud backup sync completed. Closing application.")
            return True

        elapsed = int(time.monotonic() - start_ts)
        if not wait_notice_shown and elapsed >= warn_after:
            wait_notice_shown = True
            print(
                "[shutdown] Final cloud backup is still in progress. "
                "Please wait before closing.",
                file=sys.stderr,
            )
            global _SHUTDOWN_WAIT_POPUP_SHOWN
            if not _SHUTDOWN_WAIT_POPUP_SHOWN:
                _SHUTDOWN_WAIT_POPUP_SHOWN = True
                _show_temporary_wait_popup(timeout_seconds=8)

        # Requirement: do not shut down until cloud file is available for writing.
        time.sleep(delay)


def ensure_blocking_cloud_flush_before_exit(
    *,
    status_after_seconds: int = 8,
    retry_delay_seconds: int = 3,
) -> bool:
    """Run one-time blocking cloud flush + lock release for shutdown paths.

    This function is idempotent and safe to call from multiple shutdown hooks
    (atexit, signal handlers, explicit /exit route). The first caller performs
    the blocking flush; subsequent callers return immediately.
    """
    global _SHUTDOWN_SYNC_COMPLETED

    with _SHUTDOWN_SYNC_LOCK:
        if _SHUTDOWN_SYNC_COMPLETED:
            return True
        _SHUTDOWN_SYNC_COMPLETED = True

    ok = False
    try:
        ok = flush_cloud_backup_on_shutdown(
            status_after_seconds=status_after_seconds,
            retry_delay_seconds=retry_delay_seconds,
        )
        return ok
    finally:
        try:
            release_gdrive_lock(GDRIVE_SYNC_PATH)
        except Exception as exc:
            print(f"[shutdown] WARNING: failed to release cloud lock: {exc}", file=sys.stderr)


# ====================================================================
# Module-level initialisation
# ====================================================================

_cfg = _read_db_config()
_CLOUD_PROVIDER, _CLOUD_SYNC_PATH = _resolve_cloud_provider_and_path(_cfg)
GDRIVE_SYNC_PATH: str | None = _CLOUD_SYNC_PATH or None
_SYNC_INTERVAL = _FIXED_SYNC_INTERVAL_MINUTES

DB_PATH = _resolve_db_path()

def _initialize_cloud_sync_runtime() -> None:
    """Start cloud sync runtime without holding the cloud lease open."""
    global _CLOUD_SYNC_RUNTIME_READY
    if _CLOUD_SYNC_RUNTIME_READY:
        return

    if not GDRIVE_SYNC_PATH:
        print("[sync] Startup override skipped: cloud backup path is not configured.")
        _CLOUD_SYNC_RUNTIME_READY = True
        return

    startup_pull_from_gdrive = bool(_cfg.get("startup_pull_from_gdrive", False))
    if startup_pull_from_gdrive:
        pulled = sync_from_gdrive(DB_PATH, GDRIVE_SYNC_PATH, force=True)
        if not pulled:
            print(f"[sync] Startup override skipped: {_cloud_label(GDRIVE_SYNC_PATH)} DB not found/unavailable.")

    _start_background_sync(DB_PATH, GDRIVE_SYNC_PATH, _SYNC_INTERVAL)
    _CLOUD_SYNC_RUNTIME_READY = True
    print("[sync] Cloud sync runtime started.")

# On clean exit: release lock then do one final push
def _sync_on_exit():
    """
    Called by atexit.
    Blocks shutdown until the latest local DB snapshot is successfully pushed
    to cloud backup, then releases the cloud lock.
    """
    ensure_blocking_cloud_flush_before_exit(
        status_after_seconds=8,
        retry_delay_seconds=3,
    )


atexit.register(_sync_on_exit)

def init_db():
    db_parent = os.path.dirname(DB_PATH)
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)

    # Start cloud sync runtime; lease is acquired only around sync events.
    _initialize_cloud_sync_runtime()

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
            book_loaned INTEGER DEFAULT 0,
            email TEXT,
            phone TEXT,
            guardian TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            el INTEGER DEFAULT 0,
            pi INTEGER DEFAULT 0,
            v INTEGER DEFAULT 0,
            day1 TEXT DEFAULT '',
            day1_time TEXT DEFAULT '',
            day2 TEXT DEFAULT '',
            day2_time TEXT DEFAULT '',
            day3 TEXT DEFAULT '',
            day3_time TEXT DEFAULT '',
            day4 TEXT DEFAULT '',
            day4_time TEXT DEFAULT '',
            day5 TEXT DEFAULT '',
            day5_time TEXT DEFAULT '',
            day6 TEXT DEFAULT '',
            day6_time TEXT DEFAULT '',
            checkout_notify_enabled INTEGER DEFAULT 1
        )
    """)

    # Staff
    c.execute("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            role TEXT,
            email TEXT,
            phone TEXT,
            loading INTEGER DEFAULT 1
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

    # Devices (QR-based inventory, mirrors books module behavior)
    c.execute("""
        CREATE TABLE IF NOT EXISTS materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            author TEXT,
            qr_code TEXT,
            publisher TEXT,
            available INTEGER DEFAULT 1,
            reading_level TEXT,
            copies INTEGER DEFAULT 1,
            borrower_id INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS material_loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            checkout_date TEXT NOT NULL,
            return_date TEXT,
            FOREIGN KEY(material_id) REFERENCES materials(id),
            FOREIGN KEY(student_id) REFERENCES students(id)
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

    # Cross-machine compatibility metadata
    _ensure_app_metadata_table(conn)

    conn.commit()

    # Sample data
    if not c.execute("SELECT COUNT(*) FROM students").fetchone()[0]:
        demo = [
            ("Alice Johnson", "Reading", 0, "alice@demo.com", "111-222", 1),
            ("Bob Smith", "Math", 1, "bob@demo.com", "222-333", 1)
        ]
        c.executemany("""INSERT INTO students
            (name, subject, book_loaned, email, phone, active)
            VALUES (?,?,?,?,?,?)""", demo)

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
        if "day3" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day3 TEXT")
        if "day3_time" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day3_time TEXT")
        if "day4" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day4 TEXT")
        if "day4_time" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day4_time TEXT")
        if "day5" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day5 TEXT")
        if "day5_time" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day5_time TEXT")
        if "day6" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day6 TEXT")
        if "day6_time" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN day6_time TEXT")
        if "subjects_json" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN subjects_json TEXT DEFAULT '[]'")
        if "subject_minutes_json" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN subject_minutes_json TEXT DEFAULT '[]'")
        if "total_study_minutes" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN total_study_minutes INTEGER DEFAULT 30")
        if "photo_blob" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN photo_blob BLOB")
        if "photo_mime" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN photo_mime TEXT DEFAULT ''")
        if "schedule_json" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN schedule_json TEXT DEFAULT ''")
        if "guardian" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN guardian TEXT DEFAULT ''")
        if "qr_code" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN qr_code BLOB")
        if "device_loaned" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN device_loaned INTEGER DEFAULT 0")
        if "ind" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN ind INTEGER DEFAULT 0")
        if "checkout_notify_enabled" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN checkout_notify_enabled INTEGER DEFAULT 1")
        cur.execute("UPDATE students SET checkout_notify_enabled = 1 WHERE checkout_notify_enabled IS NULL")

        schedule_rows = cur.execute(
            """
            SELECT
                id,
                COALESCE(schedule_json, ''),
                COALESCE(day1, ''), COALESCE(day1_time, ''),
                COALESCE(day2, ''), COALESCE(day2_time, ''),
                COALESCE(day3, ''), COALESCE(day3_time, ''),
                COALESCE(day4, ''), COALESCE(day4_time, ''),
                COALESCE(day5, ''), COALESCE(day5_time, ''),
                COALESCE(day6, ''), COALESCE(day6_time, '')
            FROM students
            """
        ).fetchall()

        schedule_updates = []
        for row in schedule_rows:
            student_id = row[0]
            raw_schedule_json = row[1]
            entries = []
            seen_days = set()

            if raw_schedule_json:
                try:
                    parsed = json.loads(raw_schedule_json)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = []
                if isinstance(parsed, list):
                    for entry in parsed:
                        if not isinstance(entry, dict):
                            continue
                        day = str(entry.get("day") or "").strip()
                        time = str(entry.get("time") or "").strip()
                        if not day or day in seen_days:
                            continue
                        seen_days.add(day)
                        entries.append((day, time))
                        if len(entries) >= 6:
                            break

            if not entries:
                for idx in range(2, 14, 2):
                    day = str(row[idx] or "").strip()
                    time = str(row[idx + 1] or "").strip()
                    if not day or day in seen_days:
                        continue
                    seen_days.add(day)
                    entries.append((day, time))
                    if len(entries) >= 6:
                        break

            slot_values = []
            for slot_index in range(6):
                if slot_index < len(entries):
                    slot_values.extend([entries[slot_index][0], entries[slot_index][1]])
                else:
                    slot_values.extend(["", ""])

            schedule_updates.append((*slot_values, student_id))

        if schedule_updates:
            cur.executemany(
                """
                UPDATE students
                SET
                    day1=?, day1_time=?,
                    day2=?, day2_time=?,
                    day3=?, day3_time=?,
                    day4=?, day4_time=?,
                    day5=?, day5_time=?,
                    day6=?, day6_time=?
                WHERE id=?
                """,
                schedule_updates,
            )

        # Sync device_loaned from active material loans (fixes pre-migration stale data)
        cur.execute("UPDATE students SET device_loaned = 0 WHERE device_loaned IS NULL OR device_loaned = 0")
        cur.execute("""
            UPDATE students SET device_loaned = 1
            WHERE id IN (
                SELECT DISTINCT borrower_id FROM materials
                WHERE borrower_id IS NOT NULL AND available = 0
            )
        """)
        if "paper_ws" in cols:
            try:
                cur.execute("ALTER TABLE students DROP COLUMN paper_ws")
            except Exception:
                pass
        conn.commit()

    _migrate_student_photos_to_blob(DB_PATH)
    _remove_legacy_student_columns(DB_PATH)

    # Ensure required columns exist on staff table; drop orphaned columns
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(staff)")
        cols = [r[1] for r in cur.fetchall()]
        if "whatsapp" in cols:
            cur.execute("ALTER TABLE staff DROP COLUMN whatsapp")
        if "qr_code" not in cols:
            cur.execute("ALTER TABLE staff ADD COLUMN qr_code BLOB")
        if "icon_picture" not in cols:
            cur.execute("ALTER TABLE staff ADD COLUMN icon_picture BLOB")
        if "icon_picture_mime" not in cols:
            cur.execute("ALTER TABLE staff ADD COLUMN icon_picture_mime TEXT DEFAULT ''")
        if "loading" not in cols:
            cur.execute("ALTER TABLE staff ADD COLUMN loading INTEGER DEFAULT 1")
        cur.execute("UPDATE staff SET loading = 1 WHERE loading IS NULL")
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
        if "qr_code_blob" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN qr_code_blob BLOB")
        
        conn.commit()

    # Ensure devices table/columns exist (migration for devices inventory management)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                author TEXT,
                qr_code TEXT,
                publisher TEXT,
                available INTEGER DEFAULT 1,
                reading_level TEXT,
                copies INTEGER DEFAULT 1,
                borrower_id INTEGER
            )
        """)
        cur.execute("PRAGMA table_info(materials)")
        cols = [r[1] for r in cur.fetchall()]

        if "qr_code" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN qr_code TEXT")
        if "qr_code_blob" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN qr_code_blob BLOB")
        if "publisher" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN publisher TEXT")
        if "available" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN available INTEGER DEFAULT 1")
        if "reading_level" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN reading_level TEXT")
        if "copies" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN copies INTEGER DEFAULT 1")
        if "borrower_id" not in cols:
            cur.execute("ALTER TABLE materials ADD COLUMN borrower_id INTEGER REFERENCES students(id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS material_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                checkout_date TEXT NOT NULL,
                return_date TEXT,
                FOREIGN KEY(material_id) REFERENCES materials(id),
                FOREIGN KEY(student_id) REFERENCES students(id)
            )
        """)
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
                f.write("name,email,phone,guardian,M,R,W,classification\n")
                f.write("Example Student,example@example.com,123456789,Jane Doe,x,x,,Monitored\n")

    # Automatic cloud writes are intentionally not triggered here.
    # Sync is handled by the fixed 9-minute background scheduler and
    # explicit manual push/read actions.


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