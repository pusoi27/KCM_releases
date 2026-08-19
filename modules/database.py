#*****************************
#database.py   ver 05--
#*****************************

import sqlite3, os, sys, shutil, threading, time, atexit, socket, mimetypes, uuid
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
_CHECKPOINT_INTERVAL_SECONDS = 11 * 60
_APP_VERSION_META_KEY = "app_version"
_LAST_SYNC_ERROR = ""
_SHUTDOWN_SYNC_COMPLETED = False
_SHUTDOWN_WAIT_POPUP_SHOWN = False
_SHUTDOWN_SYNC_LOCK = threading.Lock()
_CLOUD_SYNC_RUNTIME_READY = False
_ONEDRIVE_MONITOR_INTERVAL_SECONDS = 20
_ONEDRIVE_QUARANTINE_DIRNAME = ".stdytime_onedrive_quarantine"
_ONEDRIVE_MONITOR_STOP_EVENT = threading.Event()
_ONEDRIVE_MONITOR_THREAD: threading.Thread | None = None
_ONEDRIVE_MONITOR_LOCK = threading.Lock()
_ONEDRIVE_QUARANTINED_FILES: dict[str, str] = {}
_CHECKPOINT_STOP_EVENT = threading.Event()
_CHECKPOINT_THREAD: threading.Thread | None = None
_MAILBOX_SYNC_INTERVAL_MINUTES = 10
_STATION_MAILBOX_STOP_EVENT = threading.Event()
_STATION_MAILBOX_THREAD: threading.Thread | None = None
_MAILBOX_LAST_STAFF_EXPORT_HASH = ""
_MAILBOX_LAST_REFERENCE_EXPORT_HASH = ""
_DB_HEALTH_STALE_AFTER_MINUTES = 25
_SNAPSHOT_TIMER_STOP_EVENT = threading.Event()
_SNAPSHOT_TIMER_THREAD: threading.Thread | None = None
_SNAPSHOT_INTERVAL_MINUTES_DEFAULT = 15

# Adaptive retry mechanism for OneDrive sync
_SYNC_FAILURE_COUNT = 0
_SYNC_LAST_FAILURE_TIME = 0.0
_SYNC_RETRY_LOCK = threading.RLock()
_SYNC_RETRY_DELAYS = [45, 90, 180]  # seconds: 45s, 90s, 180s (exponential backoff)


def _get_next_sync_retry_delay() -> int:
    """Calculate retry delay based on consecutive failures.
    
    Returns:
        int: Delay in seconds. If all exponential backoffs exhausted, returns -1 to signal
             waiting until next 9-minute cycle.
    """
    with _SYNC_RETRY_LOCK:
        if _SYNC_FAILURE_COUNT == 0:
            return 0  # No delay, sync normally
        elif _SYNC_FAILURE_COUNT <= len(_SYNC_RETRY_DELAYS):
            return _SYNC_RETRY_DELAYS[_SYNC_FAILURE_COUNT - 1]
        else:
            return -1  # Signal: wait until next 9-min cycle


def _record_sync_failure() -> None:
    """Record a sync failure and increment retry counter."""
    global _SYNC_FAILURE_COUNT, _SYNC_LAST_FAILURE_TIME
    with _SYNC_RETRY_LOCK:
        _SYNC_FAILURE_COUNT += 1
        _SYNC_LAST_FAILURE_TIME = time.time()
        print(
            f"[sync] Failure #{_SYNC_FAILURE_COUNT} recorded at {datetime.now().strftime('%H:%M:%S')}. "
            f"Next retry: ",
            end="",
            file=sys.stderr,
        )
        delay = _get_next_sync_retry_delay()
        if delay == -1:
            print("waiting until next 9-minute cycle.", file=sys.stderr)
        elif delay == 0:
            print("immediate.", file=sys.stderr)
        else:
            print(f"{delay}s.", file=sys.stderr)


def _reset_sync_retry_state() -> None:
    """Reset retry counter and timestamp on successful sync."""
    global _SYNC_FAILURE_COUNT, _SYNC_LAST_FAILURE_TIME
    with _SYNC_RETRY_LOCK:
        if _SYNC_FAILURE_COUNT > 0:
            print(f"[sync] Retry state reset after {_SYNC_FAILURE_COUNT} failure(s).")
        _SYNC_FAILURE_COUNT = 0
        _SYNC_LAST_FAILURE_TIME = 0.0


def _should_attempt_sync_now(current_time: float) -> bool:
    """Determine if a sync attempt should be made based on retry state.
    
    Args:
        current_time: Current time (from time.time())
    
    Returns:
        bool: True if retry delay has elapsed or no failures occurred.
    """
    with _SYNC_RETRY_LOCK:
        if _SYNC_FAILURE_COUNT == 0:
            return True  # No failures, attempt normally
        
        delay = _get_next_sync_retry_delay()
        if delay == -1:
            # All exponential backoffs exhausted; wait until next 9-min cycle
            return False
        
        elapsed = current_time - _SYNC_LAST_FAILURE_TIME
        return elapsed >= delay


def _set_last_sync_error(message: str) -> None:
    global _LAST_SYNC_ERROR
    _LAST_SYNC_ERROR = str(message or "").strip()


def get_last_sync_error() -> str:
    """Return the most recent sync failure reason for UI feedback."""
    return _LAST_SYNC_ERROR


def get_sync_retry_status() -> dict:
    """Return current sync retry state for UI/diagnostic feedback.
    
    Returns:
        dict with keys:
        - "failure_count": int, number of consecutive failures
        - "next_retry_seconds": int, seconds until next retry attempt (-1 = wait for next 9-min cycle)
        - "seconds_since_failure": float, seconds elapsed since last failure (0 if no failures)
        - "status": str, human-readable status message
    """
    with _SYNC_RETRY_LOCK:
        if _SYNC_FAILURE_COUNT == 0:
            return {
                "failure_count": 0,
                "next_retry_seconds": 0,
                "seconds_since_failure": 0.0,
                "status": "No failures; sync is normal.",
            }
        
        seconds_since = time.time() - _SYNC_LAST_FAILURE_TIME
        delay = _get_next_sync_retry_delay()
        
        if delay == -1:
            next_retry = -1
            status = f"Exponential backoffs exhausted after {_SYNC_FAILURE_COUNT} failures. Waiting for next 9-minute cycle."
        else:
            next_retry = max(0, delay - int(seconds_since))
            status = f"Retry #{_SYNC_FAILURE_COUNT}: waiting {next_retry}s before next attempt (backoff: {delay}s)."
        
        return {
            "failure_count": _SYNC_FAILURE_COUNT,
            "next_retry_seconds": next_retry,
            "seconds_since_failure": seconds_since,
            "status": status,
        }


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


def _normalize_station_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"scanner_api_client", "instructor_server"}:
        return mode
    return "instructor_server"


def _normalize_backup_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in {"instructor_snapshots_only", "legacy_cloud_sync"}:
        return mode
    return "instructor_snapshots_only"


def _station_runtime_config_from_cfg(cfg: dict) -> dict:
    station_mode = _normalize_station_mode(cfg.get("station_mode", "instructor_server"))
    backup_mode = _normalize_backup_mode(cfg.get("backup_mode", "instructor_snapshots_only"))
    instructor_api_base_url = str(cfg.get("instructor_api_base_url", "") or "").strip()
    station_pairing_token = str(cfg.get("station_pairing_token", "") or "").strip()
    snapshot_interval_minutes = _safe_int(
        cfg.get("snapshot_interval_minutes"),
        default=_SNAPSHOT_INTERVAL_MINUTES_DEFAULT,
        minimum=5,
    )
    return {
        "station_mode": station_mode,
        "backup_mode": backup_mode,
        "instructor_api_base_url": instructor_api_base_url,
        "station_pairing_token": station_pairing_token,
        "snapshot_interval_minutes": snapshot_interval_minutes,
    }


def get_station_runtime_config() -> dict:
    """Return station runtime topology config from db_config.json."""
    cfg = _read_db_config()
    runtime = _station_runtime_config_from_cfg(cfg)

    # Single-machine license: always behave as combined local station,
    # regardless of stale values left in persisted config.
    activation_limit, _ = _read_station_sync_state()
    if activation_limit < 2:
        runtime["station_mode"] = "instructor_server"
        runtime["instructor_api_base_url"] = ""
        runtime["station_pairing_token"] = ""

    return runtime


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
    runtime = get_station_runtime_config()
    station_mode = runtime["station_mode"]
    backup_mode = runtime["backup_mode"]
    instructor_api_base_url = runtime["instructor_api_base_url"]
    station_pairing_token = runtime["station_pairing_token"]
    snapshot_interval_minutes = runtime["snapshot_interval_minutes"]

    issues: list[str] = []
    warnings: list[str] = []

    usable, reason = _can_use_db_parent(db_path)
    if not usable:
        issues.append(f"Local database path is not writable: {reason}")

    if backup_mode == "legacy_cloud_sync":
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
    else:
        if station_mode == "scanner_api_client":
            if not instructor_api_base_url:
                issues.append("Scanner API Client mode requires Instructor API URL.")
            if not station_pairing_token:
                issues.append("Scanner API Client mode requires a pairing token.")

    health = get_database_health_report(mode="quick_check")

    return {
        "is_ready": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "health": health,
        "config": {
            "db_path": db_path,
            "cloud_provider": cloud_provider,
            "cloud_sync_path": cloud_sync_path,
            "gdrive_sync_path": gdrive_sync_path,
            "onedrive_sync_path": onedrive_sync_path,
            "sync_interval_minutes": _FIXED_SYNC_INTERVAL_MINUTES,
            "startup_pull_from_gdrive": bool(cfg.get("startup_pull_from_gdrive", False)),
            "station_mode": station_mode,
            "backup_mode": backup_mode,
            "instructor_api_base_url": instructor_api_base_url,
            "station_pairing_token": station_pairing_token,
            "snapshot_interval_minutes": snapshot_interval_minutes,
        },
        "example": {
            "db_path": "C:/Users/YourName/AppData/Local/StdyTime/Stdytime.db",
            "onedrive_sync_path": "C:/Users/YourName/OneDrive/StdyTime",
            "instructor_api_base_url": "http://192.168.1.50:5000",
        },
    }


def save_db_config_paths(
    *,
    db_path: str | None,
    gdrive_sync_path: str,
    onedrive_sync_path: str = "",
    cloud_provider: str = "onedrive",
    station_mode: str = "instructor_server",
    backup_mode: str = "instructor_snapshots_only",
    instructor_api_base_url: str = "",
    station_pairing_token: str = "",
    snapshot_interval_minutes: int = _SNAPSHOT_INTERVAL_MINUTES_DEFAULT,
) -> dict:
    """Persist db_path and cloud sync path settings to db_config.json and return updated status."""
    db_path = _normalize_path(db_path or "")
    gdrive_sync_path = ""
    onedrive_sync_path = _normalize_path(onedrive_sync_path or "")
    cloud_provider = "onedrive"
    station_mode = _normalize_station_mode(station_mode)
    backup_mode = _normalize_backup_mode(backup_mode)
    instructor_api_base_url = str(instructor_api_base_url or "").strip()
    station_pairing_token = str(station_pairing_token or "").strip()
    snapshot_interval_minutes = _safe_int(
        snapshot_interval_minutes,
        default=_SNAPSHOT_INTERVAL_MINUTES_DEFAULT,
        minimum=5,
    )

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
    cfg["station_mode"] = station_mode
    cfg["backup_mode"] = backup_mode
    cfg["instructor_api_base_url"] = instructor_api_base_url
    cfg["station_pairing_token"] = station_pairing_token
    cfg["snapshot_interval_minutes"] = snapshot_interval_minutes

    _write_db_config(cfg)

    # Refresh the module-level cloud path so restore/backup calls in this
    # process see the newly-saved value without requiring a restart.
    global GDRIVE_SYNC_PATH
    _, _refreshed_cloud_path = _resolve_cloud_provider_and_path(cfg)
    GDRIVE_SYNC_PATH = _refreshed_cloud_path or None

    return get_db_config_status()


def _can_use_db_parent(path):
    """Return (is_usable, reason)."""
    parent = os.path.dirname(path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as exc:
        return False, str(exc)

    try:
        fd, probe_path = tempfile.mkstemp(prefix=".stdytime_db_write_probe_", dir=parent)
        with os.fdopen(fd, "w", encoding="utf-8") as probe:
            probe.write("ok")
        _safe_remove_file(probe_path, context="db write probe", silent=True)
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


def _ensure_qr_registry_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS qr_token_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            owner_type TEXT NOT NULL,
            owner_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            retired INTEGER DEFAULT 0,
            retired_at TEXT,
            UNIQUE(token)
        )
        """
    )


def register_qr_token(token: str, owner_type: str, owner_id: int | None = None, *, retired: int = 0) -> bool:
    """Register a QR token as consumed globally (never reusable)."""
    value = str(token or '').strip()
    kind = str(owner_type or '').strip().lower()
    if not value or not kind:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure_qr_registry_table(conn)
            conn.execute(
                """
                INSERT OR IGNORE INTO qr_token_registry (token, owner_type, owner_id, retired)
                VALUES (?, ?, ?, ?)
                """,
                (value, kind, owner_id, 1 if retired else 0),
            )
            conn.commit()
        return True
    except Exception:
        return False


def qr_token_exists(token: str) -> bool:
    value = str(token or '').strip()
    if not value:
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure_qr_registry_table(conn)
            row = conn.execute(
                "SELECT 1 FROM qr_token_registry WHERE token = ? LIMIT 1",
                (value,),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def issue_unique_qr_token(prefix: str, owner_type: str, owner_id: int | None = None) -> str:
    """Issue globally unique, never-before-used token and reserve it in registry."""
    safe_prefix = str(prefix or 'QR').strip().upper() or 'QR'
    kind = str(owner_type or 'unknown').strip().lower() or 'unknown'

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_qr_registry_table(conn)
        for _ in range(64):
            candidate = f"{safe_prefix}-{uuid.uuid4().hex[:12].upper()}"
            try:
                conn.execute(
                    """
                    INSERT INTO qr_token_registry (token, owner_type, owner_id, retired)
                    VALUES (?, ?, ?, 0)
                    """,
                    (candidate, kind, owner_id),
                )
                conn.commit()
                return candidate
            except sqlite3.IntegrityError:
                continue

    raise RuntimeError("Unable to issue a unique QR token after multiple attempts.")


def retire_owner_qr_tokens(owner_type: str, owner_id: int, exclude_token: str | None = None) -> None:
    """Retire all active QR tokens for an owner, optionally keeping one active token."""
    kind = str(owner_type or '').strip().lower()
    if not kind or not owner_id:
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            _ensure_qr_registry_table(conn)
            if exclude_token:
                conn.execute(
                    "UPDATE qr_token_registry SET retired=1 WHERE owner_type=? AND owner_id=? AND retired=0 AND token!=?",
                    (kind, owner_id, exclude_token),
                )
            else:
                conn.execute(
                    "UPDATE qr_token_registry SET retired=1 WHERE owner_type=? AND owner_id=? AND retired=0",
                    (kind, owner_id),
                )
            conn.commit()
    except Exception:
        pass


def _db_health_backup_dir() -> str:
    """Return persistent directory for emergency DB health snapshots."""
    return os.path.join(_CONFIG_DIR, "backups", "db_health")


def _sanitize_backup_label(label: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(label or "manual"))
    return token.strip("_") or "manual"


def _is_probable_corruption_error(error_text: str) -> bool:
    raw = str(error_text or "").strip().lower()
    return any(
        marker in raw
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "malformed",
            "not a database",
        )
    )


def _check_sqlite_db_file(db_path: str, *, label: str, mode: str = "quick_check") -> dict:
    """Run a lightweight SQLite health check for one DB file."""
    checked_at = datetime.now(timezone.utc).isoformat()
    normalized_mode = str(mode or "quick_check").strip().lower()
    pragma_mode = "integrity_check" if normalized_mode == "integrity_check" else "quick_check"

    target = str(db_path or "").strip()
    status = {
        "label": str(label or "database"),
        "path": target,
        "configured": bool(target),
        "exists": False,
        "healthy": None,
        "status": "unconfigured",
        "mode": pragma_mode,
        "result": "",
        "error": "",
        "probable_corruption": False,
        "checked_at": checked_at,
        "size_bytes": 0,
    }

    if not target:
        return status

    if not os.path.exists(target):
        status["status"] = "missing"
        return status

    status["exists"] = True
    try:
        status["size_bytes"] = int(os.path.getsize(target) or 0)
    except Exception:
        status["size_bytes"] = 0

    try:
        with sqlite3.connect(target, timeout=15) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute(f"PRAGMA {pragma_mode}").fetchone()
            result = str((row[0] if row else "") or "").strip()
            status["result"] = result
            if result.lower() != "ok":
                status["healthy"] = False
                status["status"] = "corrupt"
                status["error"] = f"PRAGMA {pragma_mode} returned '{result or 'unknown'}'"
                status["probable_corruption"] = True
                return status

            # Additional simple metadata query to surface obvious parse/open issues.
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 1",
                (),
            ).fetchone()

        status["healthy"] = True
        status["status"] = "ok"
        return status
    except Exception as exc:
        message = str(exc)
        status["healthy"] = False
        status["status"] = "error"
        status["error"] = message
        status["probable_corruption"] = _is_probable_corruption_error(message)
        return status


def _onedrive_monitor_keep_filenames(sync_target: str) -> set[str]:
    """Return lowercase file names that should remain in the cloud folder."""
    db_name = os.path.basename(sync_target or "").strip()
    if not db_name:
        db_name = "Stdytime.db"
    db_name_l = db_name.lower()
    db_base = os.path.splitext(db_name_l)[0]
    return {
        db_name_l,
        f"{db_name_l}.syncing",
        f"{db_base}.lock",
    }


def _onedrive_quarantine_non_primary_files(gdrive_sync_path: str) -> int:
    """Move non-primary cloud files into a temporary quarantine folder.

    Returns the number of files moved in this pass.
    """
    sync_target = _resolve_gdrive_sync_target(gdrive_sync_path or "")
    if not sync_target:
        return 0

    cloud_dir = os.path.dirname(sync_target)
    if not cloud_dir or not os.path.isdir(cloud_dir):
        return 0

    keep_names = _onedrive_monitor_keep_filenames(sync_target)
    quarantine_dir = os.path.join(cloud_dir, _ONEDRIVE_QUARANTINE_DIRNAME)
    os.makedirs(quarantine_dir, exist_ok=True)

    moved = 0
    with _ONEDRIVE_MONITOR_LOCK:
        for entry in os.scandir(cloud_dir):
            if not entry.is_file():
                continue

            file_name = entry.name
            file_name_l = file_name.lower()
            if file_name_l in keep_names:
                continue

            src = entry.path
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            quarantine_name = f"{ts}__{file_name}"
            dst = os.path.join(quarantine_dir, quarantine_name)
            try:
                shutil.move(src, dst)
                _ONEDRIVE_QUARANTINED_FILES[src] = dst
                moved += 1
            except Exception as exc:
                print(
                    f"[onedrive-monitor] WARNING: failed to quarantine '{src}': {exc}",
                    file=sys.stderr,
                )

    if moved:
        print(f"[onedrive-monitor] Quarantined {moved} file(s) in {cloud_dir}")
    return moved


def restore_onedrive_quarantined_files() -> int:
    """Restore files previously quarantined by the OneDrive monitor.

    Returns count of files restored.
    """
    restored = 0
    with _ONEDRIVE_MONITOR_LOCK:
        items = list(_ONEDRIVE_QUARANTINED_FILES.items())
        for original_path, quarantined_path in items:
            try:
                if not os.path.exists(quarantined_path):
                    _ONEDRIVE_QUARANTINED_FILES.pop(original_path, None)
                    continue
                if os.path.exists(original_path):
                    # Keep quarantine artifact to avoid overwriting newer files.
                    continue

                original_parent = os.path.dirname(original_path) or "."
                os.makedirs(original_parent, exist_ok=True)
                shutil.move(quarantined_path, original_path)
                _ONEDRIVE_QUARANTINED_FILES.pop(original_path, None)
                restored += 1
            except Exception as exc:
                print(
                    f"[onedrive-monitor] WARNING: failed to restore '{original_path}': {exc}",
                    file=sys.stderr,
                )

    if restored:
        print(f"[onedrive-monitor] Restored {restored} quarantined file(s)")
    return restored


def _start_onedrive_folder_monitor(gdrive_sync_path: str) -> None:
    """Continuously quarantine non-primary OneDrive files while app runs."""
    global _ONEDRIVE_MONITOR_THREAD

    sync_target = _resolve_gdrive_sync_target(gdrive_sync_path or "")
    if not sync_target:
        return

    if _ONEDRIVE_MONITOR_THREAD and _ONEDRIVE_MONITOR_THREAD.is_alive():
        return

    _ONEDRIVE_MONITOR_STOP_EVENT.clear()

    def _loop() -> None:
        # Run one pass immediately on startup.
        _onedrive_quarantine_non_primary_files(gdrive_sync_path)

        while not _ONEDRIVE_MONITOR_STOP_EVENT.wait(_ONEDRIVE_MONITOR_INTERVAL_SECONDS):
            _onedrive_quarantine_non_primary_files(gdrive_sync_path)

    _ONEDRIVE_MONITOR_THREAD = threading.Thread(
        target=_loop,
        daemon=True,
        name="onedrive-folder-monitor",
    )
    _ONEDRIVE_MONITOR_THREAD.start()
    print(f"[onedrive-monitor] Started monitor for {_resolve_gdrive_sync_target(gdrive_sync_path)}")


def stop_onedrive_folder_monitor() -> None:
    """Stop OneDrive monitor loop (best effort)."""
    _ONEDRIVE_MONITOR_STOP_EVENT.set()


def run_manual_wal_checkpoint(mode: str = "FULL") -> bool:
    """Run a manual SQLite WAL checkpoint against the local DB.

    Returns True when checkpoint command executes successfully.
    """
    checkpoint_mode = str(mode or "FULL").strip().upper() or "FULL"
    if checkpoint_mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        checkpoint_mode = "FULL"

    try:
        with sqlite3.connect(DB_PATH, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute(f"PRAGMA wal_checkpoint({checkpoint_mode})").fetchone()
        print(
            f"[checkpoint] Manual WAL checkpoint ({checkpoint_mode}) executed: result={row}",
            file=sys.stderr,
        )
        return True
    except Exception as exc:
        print(
            f"[checkpoint] WARNING: manual WAL checkpoint ({checkpoint_mode}) failed: {exc}",
            file=sys.stderr,
        )
        return False


def _start_manual_checkpoint_timer(interval_seconds: int = _CHECKPOINT_INTERVAL_SECONDS) -> None:
    """Run periodic manual WAL checkpoints while the app process is alive."""
    global _CHECKPOINT_THREAD

    if _CHECKPOINT_THREAD and _CHECKPOINT_THREAD.is_alive():
        return

    wait_seconds = max(60, int(interval_seconds))
    _CHECKPOINT_STOP_EVENT.clear()

    def _loop() -> None:
        while not _CHECKPOINT_STOP_EVENT.wait(wait_seconds):
            run_manual_wal_checkpoint("FULL")

    _CHECKPOINT_THREAD = threading.Thread(
        target=_loop,
        daemon=True,
        name="sqlite-manual-checkpoint",
    )
    _CHECKPOINT_THREAD.start()
    print(f"[checkpoint] Manual WAL checkpoint timer started (every {wait_seconds // 60} min)")


def stop_manual_checkpoint_timer() -> None:
    """Stop periodic manual checkpoint loop (best effort)."""
    _CHECKPOINT_STOP_EVENT.set()


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


def _read_station_sync_state() -> tuple[int, str]:
    """Return (activation_limit, station_role) from local app_license row."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT activation_limit, station_role FROM app_license WHERE id = 1 LIMIT 1"
            ).fetchone()
            if not row:
                return 0, ""
            activation_limit = int(row["activation_limit"] or 0)
            role = str(row["station_role"] or "").strip().lower()
            if role not in {"checkin", "instructor"}:
                role = ""
            return activation_limit, role
    except Exception:
        return 0, ""


def is_station_mailbox_mode_enabled() -> bool:
    """True only when a multi-station role is explicitly selected for Inbox/Outbox sync."""
    activation_limit, role = _read_station_sync_state()
    if activation_limit < 2:
        return False
    return role in {"checkin", "instructor"}


def _onedrive_mailbox_root(sync_path: str) -> str:
    """Return folder root used for mailbox-style sync."""
    raw = _normalize_path(sync_path)
    if not raw:
        return ""
    last_segment = os.path.basename(raw.rstrip("/"))
    if os.path.splitext(last_segment)[1]:
        return os.path.dirname(raw)
    return raw


def _station_mailbox_paths(sync_path: str) -> dict[str, str]:
    root = _onedrive_mailbox_root(sync_path)
    archive_root = os.path.join(root, "Archive") if root else ""
    return {
        "root": root,
        "scanner_outbox": os.path.join(root, "Scanner_Outbox") if root else "",
        "admin_outbox": os.path.join(root, "Admin_Outbox") if root else "",
        "archive_scanner": os.path.join(archive_root, "Scanner_Outbox") if root else "",
        "archive_admin": os.path.join(archive_root, "Admin_Outbox") if root else "",
        "station_status": os.path.join(root, "Station_Status") if root else "",
    }


def _ensure_station_mailbox_dirs(paths: dict[str, str]) -> bool:
    try:
        for key in ("root", "scanner_outbox", "admin_outbox", "archive_scanner", "archive_admin", "station_status"):
            target = paths.get(key, "")
            if target:
                os.makedirs(target, exist_ok=True)
        return True
    except Exception as exc:
        print(f"[mailbox-sync] WARNING: failed creating mailbox folders: {exc}", file=sys.stderr)
        return False


def _write_json_atomic(path: str, payload: dict) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="mailbox_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    finally:
        _safe_remove_file(tmp_path, context="mailbox temp", silent=True)


def _station_machine_id() -> str:
    return (socket.gethostname() or "unknown-machine").strip().lower()


def _scanner_export_unsynced_rows(local_path: str, scanner_outbox_dir: str) -> bool:
    """Scanner station exports unsynced session/staff-duty rows to OneDrive outbox.

    Rows are marked synced only after ACK payloads are received from Admin_Outbox.
    """
    with sqlite3.connect(local_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        session_rows = cur.execute(
            """
            SELECT id, student_id, start_time, end_time, duration
            FROM sessions
            WHERE COALESCE(sync_synced, 0) = 0
            ORDER BY id ASC
            LIMIT 1500
            """
        ).fetchall()
        assistant_rows = cur.execute(
            """
            SELECT id, assistant_id, start_time, end_time, duration
            FROM assistant_sessions
            WHERE COALESCE(sync_synced, 0) = 0
            ORDER BY id ASC
            LIMIT 1500
            """
        ).fetchall()

        if not session_rows and not assistant_rows:
            return False

        machine_id = _station_machine_id()
        generated_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema": "stdytime-mailbox-v1",
            "kind": "scanner_events",
            "generated_at": generated_at,
            "machine_id": machine_id,
            "sessions": [
                {
                    "event_id": f"sess:{machine_id}:{row['id']}:{row['end_time'] or ''}",
                    "source_row_id": int(row["id"]),
                    "student_id": row["student_id"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "duration": row["duration"],
                }
                for row in session_rows
            ],
            "assistant_sessions": [
                {
                    "event_id": f"assist:{machine_id}:{row['id']}:{row['end_time'] or ''}",
                    "source_row_id": int(row["id"]),
                    "assistant_id": row["assistant_id"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "duration": row["duration"],
                }
                for row in assistant_rows
            ],
        }

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_file = os.path.join(scanner_outbox_dir, f"punches_{stamp}.json")
        _write_json_atomic(out_file, payload)

    print(
        f"[mailbox-sync] Scanner exported {len(session_rows)} session row(s) and "
        f"{len(assistant_rows)} staff-duty row(s) -> {scanner_outbox_dir} (awaiting ACK)"
    )
    return True


def _archive_mailbox_file(src: str, archive_dir: str) -> None:
    os.makedirs(archive_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = os.path.join(archive_dir, f"{ts}__{os.path.basename(src)}")
    shutil.move(src, dst)


def _safe_positive_ints(values) -> list[int]:
    parsed: list[int] = []
    for value in values or []:
        try:
            ivalue = int(value)
        except Exception:
            continue
        if ivalue > 0:
            parsed.append(ivalue)
    return parsed


def _safe_int(value, default: int = 0, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    if minimum is not None:
        return max(minimum, parsed)
    return parsed


def _admin_write_scanner_ack(
    admin_outbox_dir: str,
    *,
    target_machine: str,
    ack_session_rows: list[int],
    ack_assistant_rows: list[int],
    source_file: str,
) -> None:
    payload = {
        "schema": "stdytime-mailbox-v1",
        "kind": "scanner_ack",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machine_id": _station_machine_id(),
        "target_machine": target_machine,
        "source_file": os.path.basename(source_file),
        "ack_sessions": sorted(set(_safe_positive_ints(ack_session_rows))),
        "ack_assistant_sessions": sorted(set(_safe_positive_ints(ack_assistant_rows))),
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_file = os.path.join(admin_outbox_dir, f"ack_{target_machine}_{stamp}.json")
    _write_json_atomic(out_file, payload)


def _admin_import_scanner_outbox(
    local_path: str,
    scanner_outbox_dir: str,
    archive_dir: str,
    admin_outbox_dir: str,
) -> int:
    """Admin station imports scanner punch files and archives processed payloads."""
    files = [
        os.path.join(scanner_outbox_dir, name)
        for name in sorted(os.listdir(scanner_outbox_dir))
        if name.lower().endswith(".json")
    ]
    imported_files = 0
    if not files:
        return 0

    with sqlite3.connect(local_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                machine_id = str(payload.get("machine_id") or "").strip().lower()
                if not machine_id:
                    _archive_mailbox_file(path, archive_dir)
                    imported_files += 1
                    continue

                ack_session_rows: list[int] = []
                ack_assistant_rows: list[int] = []

                session_items = payload.get("sessions") or []
                for item in session_items:
                    source_row_id = int(item.get("source_row_id") or 0)
                    if source_row_id <= 0:
                        continue
                    row = cur.execute(
                        """
                        SELECT id FROM sessions
                        WHERE sync_source_machine = ? AND sync_source_row_id = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (machine_id, source_row_id),
                    ).fetchone()
                    if row:
                        cur.execute(
                            """
                            UPDATE sessions
                            SET student_id = ?, start_time = ?, end_time = ?, duration = ?
                            WHERE id = ?
                            """,
                            (
                                item.get("student_id"),
                                item.get("start_time"),
                                item.get("end_time"),
                                item.get("duration"),
                                int(row["id"]),
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO sessions (
                                student_id, start_time, end_time, duration,
                                sync_synced, sync_source_machine, sync_source_row_id
                            ) VALUES (?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                item.get("student_id"),
                                item.get("start_time"),
                                item.get("end_time"),
                                item.get("duration"),
                                machine_id,
                                source_row_id,
                            ),
                        )
                    ack_session_rows.append(source_row_id)

                assistant_items = payload.get("assistant_sessions") or []
                for item in assistant_items:
                    source_row_id = int(item.get("source_row_id") or 0)
                    if source_row_id <= 0:
                        continue
                    row = cur.execute(
                        """
                        SELECT id FROM assistant_sessions
                        WHERE sync_source_machine = ? AND sync_source_row_id = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (machine_id, source_row_id),
                    ).fetchone()
                    if row:
                        cur.execute(
                            """
                            UPDATE assistant_sessions
                            SET assistant_id = ?, start_time = ?, end_time = ?, duration = ?
                            WHERE id = ?
                            """,
                            (
                                item.get("assistant_id"),
                                item.get("start_time"),
                                item.get("end_time"),
                                item.get("duration"),
                                int(row["id"]),
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO assistant_sessions (
                                assistant_id, start_time, end_time, duration,
                                sync_synced, sync_source_machine, sync_source_row_id
                            ) VALUES (?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                item.get("assistant_id"),
                                item.get("start_time"),
                                item.get("end_time"),
                                item.get("duration"),
                                machine_id,
                                source_row_id,
                            ),
                        )
                    ack_assistant_rows.append(source_row_id)

                conn.commit()
                try:
                    _admin_write_scanner_ack(
                        admin_outbox_dir,
                        target_machine=machine_id,
                        ack_session_rows=ack_session_rows,
                        ack_assistant_rows=ack_assistant_rows,
                        source_file=path,
                    )
                except Exception as ack_exc:
                    print(
                        f"[mailbox-sync] WARNING: failed writing ACK for '{path}': {ack_exc}",
                        file=sys.stderr,
                    )
                _archive_mailbox_file(path, archive_dir)
                imported_files += 1
            except Exception as exc:
                conn.rollback()
                print(f"[mailbox-sync] WARNING: failed importing scanner file '{path}': {exc}", file=sys.stderr)

    if imported_files:
        print(f"[mailbox-sync] Admin imported and archived {imported_files} scanner payload file(s)")
    return imported_files


def _staff_payload_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, name, role, email, phone, loading
        FROM staff
        ORDER BY id ASC
        """
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "role": row["role"],
            "email": row["email"],
            "phone": row["phone"],
            "loading": row["loading"],
        }
        for row in rows
    ]


def _student_payload_rows(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            id,
            COALESCE(name, '') AS name,
            COALESCE(student_identifier, '') AS student_identifier,
            COALESCE(subject, '') AS subject,
            COALESCE(subjects_json, '[]') AS subjects_json,
            COALESCE(subject_minutes_json, '[]') AS subject_minutes_json,
            COALESCE(total_study_minutes, 30) AS total_study_minutes,
            COALESCE(email, '') AS email,
            COALESCE(phone, '') AS phone,
            COALESCE(guardian, '') AS guardian,
            COALESCE(active, 1) AS active,
            COALESCE(el, 0) AS el,
            COALESCE(pi, 0) AS pi,
            COALESCE(v, 0) AS v,
            COALESCE(ind, 0) AS ind,
            COALESCE(day1, '') AS day1,
            COALESCE(day1_time, '') AS day1_time,
            COALESCE(day2, '') AS day2,
            COALESCE(day2_time, '') AS day2_time,
            COALESCE(day3, '') AS day3,
            COALESCE(day3_time, '') AS day3_time,
            COALESCE(day4, '') AS day4,
            COALESCE(day4_time, '') AS day4_time,
            COALESCE(day5, '') AS day5,
            COALESCE(day5_time, '') AS day5_time,
            COALESCE(day6, '') AS day6,
            COALESCE(day6_time, '') AS day6_time,
            COALESCE(schedule_json, '') AS schedule_json,
            COALESCE(checkout_notify_enabled, 1) AS checkout_notify_enabled
        FROM students
        ORDER BY id ASC
        """
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": row["name"],
            "student_identifier": row["student_identifier"],
            "subject": row["subject"],
            "subjects_json": row["subjects_json"],
            "subject_minutes_json": row["subject_minutes_json"],
            "total_study_minutes": int(row["total_study_minutes"] or 30),
            "email": row["email"],
            "phone": row["phone"],
            "guardian": row["guardian"],
            "active": int(row["active"] or 0),
            "el": int(row["el"] or 0),
            "pi": int(row["pi"] or 0),
            "v": int(row["v"] or 0),
            "ind": int(row["ind"] or 0),
            "day1": row["day1"],
            "day1_time": row["day1_time"],
            "day2": row["day2"],
            "day2_time": row["day2_time"],
            "day3": row["day3"],
            "day3_time": row["day3_time"],
            "day4": row["day4"],
            "day4_time": row["day4_time"],
            "day5": row["day5"],
            "day5_time": row["day5_time"],
            "day6": row["day6"],
            "day6_time": row["day6_time"],
            "schedule_json": row["schedule_json"],
            "checkout_notify_enabled": int(row["checkout_notify_enabled"] or 1),
        }
        for row in rows
    ]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (str(table_name or "").strip(),),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    if not _table_exists(conn, table_name):
        return False
    try:
        cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return any(str(col[1] or "").strip().lower() == str(column_name or "").strip().lower() for col in cols)
    except Exception:
        return False


_APP_LICENSE_SHARED_COLUMNS = (
    "license_key",
    "licensee",
    "email",
    "issued_at",
    "expires_at",
    "metadata_json",
    "ls_instance_id",
    "ls_status",
    "ls_last_verified_at",
    "activation_limit",
    "activation_usage",
)

_APP_LICENSE_INT_COLUMNS = {
    "activation_limit",
    "activation_usage",
}


def _app_license_payload_row(conn: sqlite3.Connection) -> dict:
    """Return shared app_license fields (excluding per-machine fields)."""
    if not _table_exists(conn, "app_license"):
        return {}
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM app_license WHERE id = 1 LIMIT 1").fetchone()
    if not row:
        return {}

    keys = {str(k or "").strip().lower() for k in row.keys()}
    payload: dict[str, object] = {}
    for col in _APP_LICENSE_SHARED_COLUMNS:
        if col.lower() not in keys:
            continue
        value = row[col]
        if col in _APP_LICENSE_INT_COLUMNS:
            payload[col] = _safe_int(value, default=0, minimum=0)
        else:
            payload[col] = str(value or "").strip()
    return payload


def _scanner_apply_shared_app_license(cur: sqlite3.Cursor, payload_license: dict) -> None:
    """Apply shared app_license fields while preserving machine-specific fields."""
    if not payload_license or not isinstance(payload_license, dict):
        return

    conn = cur.connection
    if not _table_exists(conn, "app_license"):
        return

    available_cols = [
        col for col in _APP_LICENSE_SHARED_COLUMNS
        if _column_exists(conn, "app_license", col)
    ]
    if not available_cols:
        return

    updates: dict[str, object] = {}
    for col in available_cols:
        if col not in payload_license:
            continue
        raw_value = payload_license.get(col)
        if col in _APP_LICENSE_INT_COLUMNS:
            updates[col] = _safe_int(raw_value, default=0, minimum=0)
        else:
            updates[col] = str(raw_value or "").strip()

    if not updates:
        return

    has_updated_at = _column_exists(conn, "app_license", "updated_at")

    existing = cur.execute("SELECT id FROM app_license WHERE id = 1 LIMIT 1").fetchone()
    if existing:
        assignments = [f"{col} = ?" for col in updates.keys()]
        params = list(updates.values())
        if has_updated_at:
            assignments.append("updated_at = ?")
            params.append(datetime.now(timezone.utc).isoformat())
        params.append(1)
        cur.execute(
            f"UPDATE app_license SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
    else:
        insert_cols = ["id"] + list(updates.keys())
        insert_vals = [1] + list(updates.values())
        if has_updated_at:
            insert_cols.append("updated_at")
            insert_vals.append(datetime.now(timezone.utc).isoformat())
        placeholders = ", ".join(["?" for _ in insert_cols])
        cur.execute(
            f"INSERT INTO app_license ({', '.join(insert_cols)}) VALUES ({placeholders})",
            insert_vals,
        )


def _book_payload_rows(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "books"):
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            b.id,
            COALESCE(b.title, '') AS title,
            COALESCE(b.author, '') AS author,
            COALESCE(b.publisher, '') AS publisher,
            COALESCE(b.reading_level, '') AS reading_level,
            COALESCE(b.isbn, '') AS isbn,
            COALESCE(b.isbn13, '') AS isbn13,
            COALESCE(b.copies, 0) AS copies,
            COALESCE(b.available, 0) AS available,
            COALESCE(s.student_identifier, '') AS borrower_student_identifier,
            COALESCE(s.email, '') AS borrower_email,
            COALESCE(s.name, '') AS borrower_name,
            COALESCE(s.phone, '') AS borrower_phone
        FROM books b
        LEFT JOIN students s ON s.id = b.borrower_id
        ORDER BY b.id ASC
        """
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": row["title"],
            "author": row["author"],
            "publisher": row["publisher"],
            "reading_level": row["reading_level"],
            "isbn": row["isbn"],
            "isbn13": row["isbn13"],
            "copies": _safe_int(row["copies"], default=0, minimum=0),
            "available": 1 if _safe_int(row["available"], default=0, minimum=0) else 0,
            "borrower_student_identifier": row["borrower_student_identifier"],
            "borrower_email": row["borrower_email"],
            "borrower_name": row["borrower_name"],
            "borrower_phone": row["borrower_phone"],
        }
        for row in rows
    ]


def _material_payload_rows(conn: sqlite3.Connection) -> list[dict]:
    if not _table_exists(conn, "materials"):
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            m.id,
            COALESCE(m.title, '') AS title,
            COALESCE(m.author, '') AS author,
            COALESCE(m.publisher, '') AS publisher,
            COALESCE(m.reading_level, '') AS reading_level,
            COALESCE(m.qr_code, '') AS qr_code,
            COALESCE(m.copies, 0) AS copies,
            COALESCE(m.available, 0) AS available,
            COALESCE(s.student_identifier, '') AS borrower_student_identifier,
            COALESCE(s.email, '') AS borrower_email,
            COALESCE(s.name, '') AS borrower_name,
            COALESCE(s.phone, '') AS borrower_phone
        FROM materials m
        LEFT JOIN students s ON s.id = m.borrower_id
        ORDER BY m.id ASC
        """
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": row["title"],
            "author": row["author"],
            "publisher": row["publisher"],
            "reading_level": row["reading_level"],
            "qr_code": row["qr_code"],
            "copies": _safe_int(row["copies"], default=0, minimum=0),
            "available": 1 if _safe_int(row["available"], default=0, minimum=0) else 0,
            "borrower_student_identifier": row["borrower_student_identifier"],
            "borrower_email": row["borrower_email"],
            "borrower_name": row["borrower_name"],
            "borrower_phone": row["borrower_phone"],
        }
        for row in rows
    ]


def _scanner_find_student_id_by_anchor(cur: sqlite3.Cursor, payload_item: dict) -> int | None:
    student_identifier = str(payload_item.get("borrower_student_identifier") or "").strip()
    email = str(payload_item.get("borrower_email") or "").strip()
    name = str(payload_item.get("borrower_name") or "").strip()
    phone = str(payload_item.get("borrower_phone") or "").strip()

    if student_identifier:
        row = cur.execute(
            "SELECT id FROM students WHERE LOWER(COALESCE(student_identifier,'')) = LOWER(?) LIMIT 1",
            (student_identifier,),
        ).fetchone()
        if row:
            return int(row["id"])

    if email:
        row = cur.execute(
            "SELECT id FROM students WHERE LOWER(COALESCE(email,'')) = LOWER(?) LIMIT 1",
            (email,),
        ).fetchone()
        if row:
            return int(row["id"])

    if name:
        row = cur.execute(
            "SELECT id FROM students WHERE LOWER(COALESCE(name,'')) = LOWER(?) AND LOWER(COALESCE(phone,'')) = LOWER(?) LIMIT 1",
            (name, phone),
        ).fetchone()
        if row:
            return int(row["id"])

    return None


def _scanner_upsert_books(cur: sqlite3.Cursor, payload_books: list[dict]) -> None:
    if not _table_exists(cur.connection, "books"):
        return
    for item in payload_books or []:
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or "").strip()
        publisher = str(item.get("publisher") or "").strip()
        reading_level = str(item.get("reading_level") or "").strip()
        isbn = str(item.get("isbn") or "").strip()
        isbn13 = str(item.get("isbn13") or "").strip()
        copies = _safe_int(item.get("copies"), default=0, minimum=0)
        borrower_id = _scanner_find_student_id_by_anchor(cur, item)

        existing = None
        if isbn:
            existing = cur.execute(
                "SELECT id FROM books WHERE LOWER(COALESCE(isbn,'')) = LOWER(?) OR LOWER(COALESCE(isbn13,'')) = LOWER(?) LIMIT 1",
                (isbn, isbn),
            ).fetchone()
        if not existing and isbn13:
            existing = cur.execute(
                "SELECT id FROM books WHERE LOWER(COALESCE(isbn,'')) = LOWER(?) OR LOWER(COALESCE(isbn13,'')) = LOWER(?) LIMIT 1",
                (isbn13, isbn13),
            ).fetchone()
        if not existing and title:
            existing = cur.execute(
                """
                SELECT id FROM books
                WHERE LOWER(COALESCE(title,'')) = LOWER(?)
                  AND LOWER(COALESCE(author,'')) = LOWER(?)
                  AND LOWER(COALESCE(publisher,'')) = LOWER(?)
                LIMIT 1
                """,
                (title, author, publisher),
            ).fetchone()

        computed_available = 1 if (copies > 0 and (isbn or isbn13) and not borrower_id) else 0

        if existing:
            cur.execute(
                """
                UPDATE books
                SET
                    title = ?, author = ?, publisher = ?, reading_level = ?,
                    isbn = ?, isbn13 = ?, copies = ?, available = ?, borrower_id = ?
                WHERE id = ?
                """,
                (
                    title, author, publisher, reading_level,
                    isbn, isbn13, copies, computed_available, borrower_id,
                    int(existing["id"]),
                ),
            )
        else:
            if not title:
                continue
            cur.execute(
                """
                INSERT INTO books (
                    title, author, publisher, reading_level,
                    isbn, isbn13, copies, available, borrower_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title, author, publisher, reading_level,
                    isbn, isbn13, copies, computed_available, borrower_id,
                ),
            )


def _scanner_upsert_materials(cur: sqlite3.Cursor, payload_materials: list[dict]) -> None:
    if not _table_exists(cur.connection, "materials"):
        return
    for item in payload_materials or []:
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or "").strip()
        publisher = str(item.get("publisher") or "").strip()
        reading_level = str(item.get("reading_level") or "").strip()
        qr_code = str(item.get("qr_code") or "").strip()
        copies = _safe_int(item.get("copies"), default=0, minimum=0)
        borrower_id = _scanner_find_student_id_by_anchor(cur, item)

        existing = None
        if qr_code:
            existing = cur.execute(
                "SELECT id FROM materials WHERE LOWER(COALESCE(qr_code,'')) = LOWER(?) LIMIT 1",
                (qr_code,),
            ).fetchone()
        if not existing and title:
            existing = cur.execute(
                """
                SELECT id FROM materials
                WHERE LOWER(COALESCE(title,'')) = LOWER(?)
                  AND LOWER(COALESCE(author,'')) = LOWER(?)
                  AND LOWER(COALESCE(publisher,'')) = LOWER(?)
                LIMIT 1
                """,
                (title, author, publisher),
            ).fetchone()

        computed_available = 1 if (copies > 0 and qr_code and not borrower_id) else 0

        if existing:
            cur.execute(
                """
                UPDATE materials
                SET
                    title = ?, author = ?, publisher = ?, reading_level = ?,
                    qr_code = ?, copies = ?, available = ?, borrower_id = ?
                WHERE id = ?
                """,
                (
                    title, author, publisher, reading_level,
                    qr_code, copies, computed_available, borrower_id,
                    int(existing["id"]),
                ),
            )
        else:
            if not title:
                continue
            cur.execute(
                """
                INSERT INTO materials (
                    title, author, publisher, reading_level,
                    qr_code, copies, available, borrower_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title, author, publisher, reading_level,
                    qr_code, copies, computed_available, borrower_id,
                ),
            )


def _scanner_recompute_student_loan_flags(cur: sqlite3.Cursor) -> None:
    conn = cur.connection
    if not _table_exists(conn, "students"):
        return

    has_book_loaned_col = _column_exists(conn, "students", "book_loaned")
    has_device_loaned_col = _column_exists(conn, "students", "device_loaned")

    if has_book_loaned_col:
        cur.execute("UPDATE students SET book_loaned = 0")
        if _table_exists(conn, "books"):
            cur.execute(
                """
                UPDATE students
                SET book_loaned = 1
                WHERE id IN (
                    SELECT DISTINCT borrower_id
                    FROM books
                    WHERE borrower_id IS NOT NULL
                )
                """
            )

    if has_device_loaned_col:
        cur.execute("UPDATE students SET device_loaned = 0")
        if _table_exists(conn, "materials"):
            cur.execute(
                """
                UPDATE students
                SET device_loaned = 1
                WHERE id IN (
                    SELECT DISTINCT borrower_id
                    FROM materials
                    WHERE borrower_id IS NOT NULL
                )
                """
            )


def _admin_export_staff_snapshot(local_path: str, admin_outbox_dir: str) -> bool:
    """Admin station exports staff/student/inventory snapshots for scanner refresh."""
    global _MAILBOX_LAST_REFERENCE_EXPORT_HASH
    with sqlite3.connect(local_path) as conn:
        staff_rows = _staff_payload_rows(conn)
        student_rows = _student_payload_rows(conn)
        book_rows = _book_payload_rows(conn)
        material_rows = _material_payload_rows(conn)
        app_license = _app_license_payload_row(conn)

    digest_payload = {
        "staff": staff_rows,
        "students": student_rows,
        "books": book_rows,
        "materials": material_rows,
        "app_license": app_license,
    }
    digest = uuid.uuid5(uuid.NAMESPACE_OID, json.dumps(digest_payload, sort_keys=True)).hex
    if digest == _MAILBOX_LAST_REFERENCE_EXPORT_HASH:
        return False

    payload = {
        "schema": "stdytime-mailbox-v1",
        "kind": "admin_reference",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machine_id": _station_machine_id(),
        "staff": staff_rows,
        "students": student_rows,
        "books": book_rows,
        "materials": material_rows,
        "app_license": app_license,
    }
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_file = os.path.join(admin_outbox_dir, f"reference_{stamp}.json")
    _write_json_atomic(out_file, payload)
    _MAILBOX_LAST_REFERENCE_EXPORT_HASH = digest
    print(
        "[mailbox-sync] Admin exported reference snapshot "
        f"(staff={len(staff_rows)} row(s), students={len(student_rows)} row(s), "
        f"books={len(book_rows)} row(s), devices={len(material_rows)} row(s), "
        f"license={'yes' if app_license else 'no'})"
    )
    return True


def _scanner_apply_ack_payload(conn: sqlite3.Connection, payload: dict) -> int:
    """Apply ACK payload to scanner-local rows and mark them synced."""
    local_machine = _station_machine_id()
    target_machine = str(payload.get("target_machine") or "").strip().lower()
    if target_machine and target_machine != local_machine:
        return 0

    cur = conn.cursor()
    acked_total = 0

    ack_session_rows = _safe_positive_ints(payload.get("ack_sessions") or [])
    for source_row_id in ack_session_rows:
        cur.execute(
            "UPDATE sessions SET sync_synced = 1 WHERE id = ?",
            (source_row_id,),
        )
        acked_total += int(cur.rowcount or 0)

    ack_assistant_rows = _safe_positive_ints(payload.get("ack_assistant_sessions") or [])
    for source_row_id in ack_assistant_rows:
        cur.execute(
            "UPDATE assistant_sessions SET sync_synced = 1 WHERE id = ?",
            (source_row_id,),
        )
        acked_total += int(cur.rowcount or 0)

    return acked_total


def _scanner_import_admin_outbox(local_path: str, admin_outbox_dir: str, archive_dir: str) -> int:
    """Scanner station imports Admin_Outbox payloads (reference snapshots + ACK files)."""
    files = [
        os.path.join(admin_outbox_dir, name)
        for name in sorted(os.listdir(admin_outbox_dir))
        if name.lower().endswith(".json")
    ]
    if not files:
        return 0

    imported_files = 0
    with sqlite3.connect(local_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                kind = str(payload.get("kind") or "").strip().lower()

                if kind == "scanner_ack":
                    acked = _scanner_apply_ack_payload(conn, payload)
                    conn.commit()
                    if acked:
                        print(f"[mailbox-sync] Scanner applied ACK for {acked} row(s)")
                else:
                    staff_rows = payload.get("staff") or []
                    for item in staff_rows:
                        name = str(item.get("name") or "").strip()
                        role = str(item.get("role") or "").strip()
                        email = str(item.get("email") or "").strip()
                        phone = str(item.get("phone") or "").strip()
                        loading = int(item.get("loading") or 1)
                        if not name:
                            continue

                        existing = None
                        if email:
                            existing = cur.execute(
                                "SELECT id FROM staff WHERE LOWER(COALESCE(email,'')) = LOWER(?) LIMIT 1",
                                (email,),
                            ).fetchone()
                        if not existing:
                            existing = cur.execute(
                                "SELECT id FROM staff WHERE LOWER(COALESCE(name,'')) = LOWER(?) AND LOWER(COALESCE(phone,'')) = LOWER(?) LIMIT 1",
                                (name, phone),
                            ).fetchone()

                        if existing:
                            cur.execute(
                                """
                                UPDATE staff
                                SET name = ?, role = ?, email = ?, phone = ?, loading = ?
                                WHERE id = ?
                                """,
                                (name, role, email, phone, loading, int(existing["id"])),
                            )
                        else:
                            cur.execute(
                                """
                                INSERT INTO staff (name, role, email, phone, loading)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (name, role, email, phone, loading),
                            )

                    student_rows = payload.get("students") or []
                    for item in student_rows:
                        name = str(item.get("name") or "").strip()
                        if not name:
                            continue

                        student_identifier = str(item.get("student_identifier") or "").strip()
                        subject = str(item.get("subject") or "").strip()
                        subjects_json = str(item.get("subjects_json") or "[]")
                        subject_minutes_json = str(item.get("subject_minutes_json") or "[]")
                        total_study_minutes = _safe_int(item.get("total_study_minutes"), default=30, minimum=5)
                        email = str(item.get("email") or "").strip()
                        phone = str(item.get("phone") or "").strip()
                        guardian = str(item.get("guardian") or "").strip()
                        active = _safe_int(item.get("active"), default=1, minimum=0)
                        el = 1 if _safe_int(item.get("el"), default=0, minimum=0) else 0
                        pi = 1 if _safe_int(item.get("pi"), default=0, minimum=0) else 0
                        v = 1 if _safe_int(item.get("v"), default=0, minimum=0) else 0
                        ind = 1 if _safe_int(item.get("ind"), default=0, minimum=0) else 0
                        day1 = str(item.get("day1") or "").strip()
                        day1_time = str(item.get("day1_time") or "").strip()
                        day2 = str(item.get("day2") or "").strip()
                        day2_time = str(item.get("day2_time") or "").strip()
                        day3 = str(item.get("day3") or "").strip()
                        day3_time = str(item.get("day3_time") or "").strip()
                        day4 = str(item.get("day4") or "").strip()
                        day4_time = str(item.get("day4_time") or "").strip()
                        day5 = str(item.get("day5") or "").strip()
                        day5_time = str(item.get("day5_time") or "").strip()
                        day6 = str(item.get("day6") or "").strip()
                        day6_time = str(item.get("day6_time") or "").strip()
                        schedule_json = str(item.get("schedule_json") or "").strip()
                        checkout_notify_enabled = 1 if _safe_int(item.get("checkout_notify_enabled"), default=1, minimum=0) else 0

                        existing = None
                        if student_identifier:
                            existing = cur.execute(
                                "SELECT id FROM students WHERE LOWER(COALESCE(student_identifier,'')) = LOWER(?) LIMIT 1",
                                (student_identifier,),
                            ).fetchone()
                        if not existing and email:
                            existing = cur.execute(
                                "SELECT id FROM students WHERE LOWER(COALESCE(email,'')) = LOWER(?) LIMIT 1",
                                (email,),
                            ).fetchone()
                        if not existing:
                            existing = cur.execute(
                                "SELECT id FROM students WHERE LOWER(COALESCE(name,'')) = LOWER(?) AND LOWER(COALESCE(phone,'')) = LOWER(?) LIMIT 1",
                                (name, phone),
                            ).fetchone()

                        if existing:
                            cur.execute(
                                """
                                UPDATE students
                                SET
                                    name = ?, student_identifier = ?, subject = ?,
                                    subjects_json = ?, subject_minutes_json = ?, total_study_minutes = ?,
                                    email = ?, phone = ?, guardian = ?, active = ?,
                                    el = ?, pi = ?, v = ?, ind = ?,
                                    day1 = ?, day1_time = ?, day2 = ?, day2_time = ?,
                                    day3 = ?, day3_time = ?, day4 = ?, day4_time = ?,
                                    day5 = ?, day5_time = ?, day6 = ?, day6_time = ?,
                                    schedule_json = ?, checkout_notify_enabled = ?
                                WHERE id = ?
                                """,
                                (
                                    name, student_identifier, subject,
                                    subjects_json, subject_minutes_json, total_study_minutes,
                                    email, phone, guardian, active,
                                    el, pi, v, ind,
                                    day1, day1_time, day2, day2_time,
                                    day3, day3_time, day4, day4_time,
                                    day5, day5_time, day6, day6_time,
                                    schedule_json, checkout_notify_enabled,
                                    int(existing["id"]),
                                ),
                            )
                        else:
                            cur.execute(
                                """
                                INSERT INTO students (
                                    name, student_identifier, subject,
                                    subjects_json, subject_minutes_json, total_study_minutes,
                                    email, phone, guardian, active,
                                    el, pi, v, ind,
                                    day1, day1_time, day2, day2_time,
                                    day3, day3_time, day4, day4_time,
                                    day5, day5_time, day6, day6_time,
                                    schedule_json, checkout_notify_enabled
                                )
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    name, student_identifier, subject,
                                    subjects_json, subject_minutes_json, total_study_minutes,
                                    email, phone, guardian, active,
                                    el, pi, v, ind,
                                    day1, day1_time, day2, day2_time,
                                    day3, day3_time, day4, day4_time,
                                    day5, day5_time, day6, day6_time,
                                    schedule_json, checkout_notify_enabled,
                                ),
                            )

                    _scanner_upsert_books(cur, payload.get("books") or [])
                    _scanner_upsert_materials(cur, payload.get("materials") or [])
                    _scanner_apply_shared_app_license(cur, payload.get("app_license") or {})
                    _scanner_recompute_student_loan_flags(cur)

                    conn.commit()

                _archive_mailbox_file(path, archive_dir)
                imported_files += 1
            except Exception as exc:
                conn.rollback()
                print(f"[mailbox-sync] WARNING: failed importing admin file '{path}': {exc}", file=sys.stderr)

    if imported_files:
        print(f"[mailbox-sync] Scanner imported and archived {imported_files} admin payload file(s)")
    return imported_files


def _run_station_mailbox_cycle(local_path: str, sync_path: str) -> None:
    activation_limit, role = _read_station_sync_state()
    if activation_limit < 2:
        return
    if role not in {"checkin", "instructor"}:
        return

    paths = _station_mailbox_paths(sync_path)
    if not paths.get("root") or not _ensure_station_mailbox_dirs(paths):
        return

    if role == "checkin":
        _scanner_import_admin_outbox(local_path, paths["admin_outbox"], paths["archive_admin"])
        _scanner_export_unsynced_rows(local_path, paths["scanner_outbox"])
    else:
        _admin_import_scanner_outbox(local_path, paths["scanner_outbox"], paths["archive_scanner"], paths["admin_outbox"])
        _admin_export_staff_snapshot(local_path, paths["admin_outbox"])

    try:
        _write_station_sync_heartbeat(local_path, sync_path, role)
    except Exception as exc:
        print(f"[mailbox-sync] WARNING: failed writing station heartbeat: {exc}", file=sys.stderr)


def _dataset_signature_for_db(db_path: str) -> tuple[str, int]:
    """Return a lightweight dataset signature + pending unsynced row count for a DB file."""
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            aggregates: dict[str, dict[str, int]] = {}

            for table in ("students", "staff", "books", "materials", "sessions", "assistant_sessions"):
                if not _table_exists(conn, table):
                    continue
                count = int(cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
                max_id = int(cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0] or 0)
                aggregates[table] = {
                    "count": count,
                    "max_id": max_id,
                }

            unsynced_sessions = 0
            unsynced_assistants = 0
            if _table_exists(conn, "sessions"):
                unsynced_sessions = int(
                    cur.execute("SELECT COUNT(*) FROM sessions WHERE COALESCE(sync_synced, 0) = 0").fetchone()[0] or 0
                )
            if _table_exists(conn, "assistant_sessions"):
                unsynced_assistants = int(
                    cur.execute("SELECT COUNT(*) FROM assistant_sessions WHERE COALESCE(sync_synced, 0) = 0").fetchone()[0] or 0
                )

            pending_unsynced = unsynced_sessions + unsynced_assistants
            digest_payload = {
                "aggregates": aggregates,
                "pending_unsynced": pending_unsynced,
            }
            digest = uuid.uuid5(uuid.NAMESPACE_OID, json.dumps(digest_payload, sort_keys=True)).hex
            return digest, pending_unsynced
    except Exception:
        return "", -1


def _dataset_signature_for_station(local_path: str) -> tuple[str, int]:
    """Backward-compatible alias for station-local signature calculations."""
    return _dataset_signature_for_db(local_path)


def _cloud_dataset_signature(sync_path: str) -> tuple[str, int, str]:
    """Return cloud DB signature, pending count, and optional error string."""
    target_path = _resolve_gdrive_sync_target(sync_path or "")
    if not target_path:
        return "", -1, "Cloud backup DB path is not configured."
    if not os.path.exists(target_path):
        return "", -1, f"Cloud backup DB was not found at: {target_path}"

    signature, pending = _dataset_signature_for_db(target_path)
    if not signature:
        return "", pending, "Cloud backup DB signature could not be computed."
    return signature, pending, ""


def _station_heartbeat_file(status_dir: str, role: str, machine_id: str) -> str:
    safe_role = str(role or "").strip().lower() or "unknown"
    safe_machine = str(machine_id or "").strip().lower() or "unknown-machine"
    return os.path.join(status_dir, f"{safe_role}_{safe_machine}.json")


def _write_station_sync_heartbeat(local_path: str, sync_path: str, role: str) -> dict:
    paths = _station_mailbox_paths(sync_path)
    status_dir = paths.get("station_status", "")
    if not status_dir:
        return {}
    os.makedirs(status_dir, exist_ok=True)

    machine_id = _station_machine_id()
    activation_limit, _ = _read_station_sync_state()
    signature, pending_unsynced = _dataset_signature_for_station(local_path)
    local_health = _check_sqlite_db_file(local_path, label="local", mode="quick_check")
    payload = {
        "schema": "stdytime-mailbox-v1",
        "kind": "station_heartbeat",
        "role": str(role or "").strip().lower(),
        "machine_id": machine_id,
        "activation_limit": int(activation_limit or 0),
        "dataset_signature": signature,
        "pending_unsynced": int(pending_unsynced),
        "local_db_health": str(local_health.get("status") or "unknown"),
        "local_db_health_error": str(local_health.get("error") or "").strip(),
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
    }
    out_file = _station_heartbeat_file(status_dir, role, machine_id)
    _write_json_atomic(out_file, payload)
    return payload


def _read_station_heartbeat(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _parse_utc_iso(raw: str) -> datetime | None:
    token = str(raw or "").strip()
    if not token:
        return None
    try:
        dt = datetime.fromisoformat(token)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _latest_station_heartbeat(status_dir: str, role: str) -> dict:
    role_prefix = f"{str(role or '').strip().lower()}_"
    best_payload: dict = {}
    best_ts: datetime | None = None

    try:
        for name in os.listdir(status_dir):
            if not name.lower().endswith(".json"):
                continue
            if not name.lower().startswith(role_prefix):
                continue
            payload = _read_station_heartbeat(os.path.join(status_dir, name))
            if not payload:
                continue
            ts = _parse_utc_iso(payload.get("heartbeat_at") or "")
            if not ts:
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_payload = payload
    except Exception:
        return {}

    return best_payload


def get_station_dataset_sync_status(stale_after_minutes: int = 25) -> dict:
    """Return navbar-friendly sync marker data for multi-station setups."""
    activation_limit, role = _read_station_sync_state()
    if activation_limit < 2 or role not in {"checkin", "instructor"}:
        return {
            "visible": False,
            "tone": "secondary",
            "label": "",
            "tooltip": "",
        }

    runtime = get_station_runtime_config()
    station_mode = str(runtime.get("station_mode") or "").strip().lower()
    backup_mode = str(runtime.get("backup_mode") or "").strip().lower()

    # In API topology with snapshots-only backups, mailbox folder heartbeats are not
    # a reliable signal for station connectivity; use Station Link badge instead.
    if backup_mode == "instructor_snapshots_only" and station_mode in {"scanner_api_client", "instructor_server"}:
        return {
            "visible": False,
            "tone": "secondary",
            "label": "",
            "tooltip": "",
        }

    sync_path = GDRIVE_SYNC_PATH or ""
    paths = _station_mailbox_paths(sync_path)
    status_dir = paths.get("station_status", "")
    if not status_dir:
        return {
            "visible": True,
            "tone": "warning",
            "label": "DB Sync: setup",
            "tooltip": "Cloud mailbox path is not configured.",
        }

    if not os.path.isdir(status_dir):
        return {
            "visible": True,
            "tone": "warning",
            "label": "DB Sync: waiting",
            "tooltip": "Waiting for station status folder to be created.",
        }

    local_signature, local_pending = _dataset_signature_for_station(DB_PATH)
    cloud_signature, cloud_pending, cloud_error = _cloud_dataset_signature(sync_path)
    peer_role = "instructor" if role == "checkin" else "checkin"
    peer_payload = _latest_station_heartbeat(status_dir, peer_role)

    if not peer_payload:
        return {
            "visible": True,
            "tone": "warning",
            "label": "DB Sync: waiting peer",
            "tooltip": f"No recent {peer_role} station heartbeat found yet.",
        }

    peer_signature = str(peer_payload.get("dataset_signature") or "").strip()
    peer_pending = int(peer_payload.get("pending_unsynced") or 0)
    peer_machine = str(peer_payload.get("machine_id") or "peer").strip() or "peer"
    peer_heartbeat = _parse_utc_iso(peer_payload.get("heartbeat_at") or "")
    now_utc = datetime.now(timezone.utc)
    stale_seconds = max(60, int(stale_after_minutes) * 60)
    age_seconds = int((now_utc - peer_heartbeat).total_seconds()) if peer_heartbeat else 10**9

    if age_seconds > stale_seconds:
        return {
            "visible": True,
            "tone": "warning",
            "label": "DB Sync: stale",
            "tooltip": (
                f"Peer heartbeat from {peer_machine} is stale ({age_seconds // 60} min old). "
                "Stations may not be on the same latest dataset."
            ),
        }

    if cloud_error:
        return {
            "visible": True,
            "tone": "warning",
            "label": "DB Sync: waiting cloud",
            "tooltip": cloud_error,
        }

    local_tag = local_signature[:12] if local_signature else "none"
    peer_tag = peer_signature[:12] if peer_signature else "none"
    cloud_tag = cloud_signature[:12] if cloud_signature else "none"

    in_sync = (
        bool(local_signature)
        and bool(peer_signature)
        and bool(cloud_signature)
        and local_signature == peer_signature == cloud_signature
        and int(local_pending) == 0
        and int(peer_pending) == 0
        and int(cloud_pending) == 0
    )

    if in_sync:
        return {
            "visible": True,
            "tone": "success",
            "label": "DB Sync: in sync",
            "tooltip": (
                f"All databases share sync tag {local_tag} (scanner/instructor/OneDrive) "
                f"with zero pending rows. Peer: {peer_machine}."
            ),
            "sync_tag": local_tag,
        }

    return {
        "visible": True,
        "tone": "warning",
        "label": "DB Sync: syncing",
        "tooltip": (
            "Sync in progress or identifiers differ "
            f"(local={local_tag}, peer={peer_tag}, cloud={cloud_tag}; "
            f"pending local/peer/cloud={local_pending}/{peer_pending}/{cloud_pending}; "
            f"peer={peer_machine})."
        ),
        "sync_tag": local_tag,
    }


def _start_station_mailbox_sync(local_path: str, sync_path: str, interval_minutes: int) -> None:
    """Start 10-minute Inbox/Outbox sync loop for 2-activation station deployments."""
    global _STATION_MAILBOX_THREAD

    if _STATION_MAILBOX_THREAD and _STATION_MAILBOX_THREAD.is_alive():
        return

    wait_seconds = max(60, int(interval_minutes) * 60)
    _STATION_MAILBOX_STOP_EVENT.clear()

    def _loop() -> None:
        while not _STATION_MAILBOX_STOP_EVENT.is_set():
            try:
                _run_station_mailbox_cycle(local_path, sync_path)
            except Exception as exc:
                print(f"[mailbox-sync] WARNING: mailbox cycle failed: {exc}", file=sys.stderr)
            if _STATION_MAILBOX_STOP_EVENT.wait(wait_seconds):
                break

    _STATION_MAILBOX_THREAD = threading.Thread(
        target=_loop,
        daemon=True,
        name="station-mailbox-sync",
    )
    _STATION_MAILBOX_THREAD.start()
    print(f"[mailbox-sync] Station mailbox runtime started (every {int(interval_minutes)} min)")


def stop_station_mailbox_sync() -> None:
    _STATION_MAILBOX_STOP_EVENT.set()


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


def _create_health_snapshot(db_path: str, *, source_label: str, reason: str) -> dict:
    """Create an emergency snapshot of the given DB using SQLite backup API."""
    target = str(db_path or "").strip()
    if not target:
        return {"ok": False, "path": "", "error": "No database path provided."}
    if not os.path.exists(target):
        return {"ok": False, "path": "", "error": f"Database file not found: {target}"}

    backup_dir = _db_health_backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_label = _sanitize_backup_label(source_label)
    backup_path = os.path.join(backup_dir, f"{safe_label}_{stamp}.db")

    try:
        with sqlite3.connect(target, timeout=15) as src_conn, sqlite3.connect(backup_path) as dst_conn:
            src_conn.execute("PRAGMA busy_timeout=5000")
            src_conn.backup(dst_conn)

        print(
            f"[db-health] Snapshot created for {safe_label} ({reason}): {backup_path}",
            file=sys.stderr,
        )
        return {"ok": True, "path": backup_path, "error": ""}
    except Exception as exc:
        _safe_remove_file(backup_path, context="failed db-health snapshot", silent=True)
        return {"ok": False, "path": "", "error": str(exc)}


def _restore_db_from_path(*, source_path: str, target_path: str, reason: str) -> dict:
    """Restore one SQLite file into another path via online backup API."""
    src = str(source_path or "").strip()
    dst = str(target_path or "").strip()
    if not src or not dst:
        return {
            "ok": False,
            "source": src,
            "target": dst,
            "error": "Source and target paths are required.",
            "reason": reason,
        }
    if not os.path.exists(src):
        return {
            "ok": False,
            "source": src,
            "target": dst,
            "error": f"Source database not found: {src}",
            "reason": reason,
        }

    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        _sqlite_restore_live(src, dst)
        print(f"[db-health] Restored DB ({reason}): {src} -> {dst}")
        return {
            "ok": True,
            "source": src,
            "target": dst,
            "error": "",
            "reason": reason,
        }
    except Exception as exc:
        return {
            "ok": False,
            "source": src,
            "target": dst,
            "error": str(exc),
            "reason": reason,
        }


def get_database_health_report(*, mode: str = "quick_check") -> dict:
    """Return a health report for local/peer/cloud DB topology.

    - Local: always checks current machine DB_PATH.
    - Cloud: checks configured OneDrive DB (when configured).
    - Peer: in 2-station mode, checks latest heartbeat metadata for opposite role.
    """
    activation_limit, role = _read_station_sync_state()
    station_mode = activation_limit >= 2 and role in {"checkin", "instructor"}

    local_result = _check_sqlite_db_file(DB_PATH, label="local", mode=mode)

    cloud_target = _resolve_gdrive_sync_target(GDRIVE_SYNC_PATH or "") if GDRIVE_SYNC_PATH else ""
    cloud_result = _check_sqlite_db_file(cloud_target, label="cloud", mode=mode)

    peer = {
        "role": "",
        "machine_id": "",
        "healthy": None,
        "status": "not_applicable",
        "dataset_signature": "",
        "pending_unsynced": -1,
        "age_seconds": None,
        "heartbeat_at": "",
        "note": "",
    }

    if station_mode:
        peer_role = "instructor" if role == "checkin" else "checkin"
        status_dir = _station_mailbox_paths(GDRIVE_SYNC_PATH or "").get("station_status", "")
        peer["role"] = peer_role
        if status_dir and os.path.isdir(status_dir):
            payload = _latest_station_heartbeat(status_dir, peer_role)
            if payload:
                heartbeat = _parse_utc_iso(payload.get("heartbeat_at") or "")
                now_utc = datetime.now(timezone.utc)
                age_seconds = int((now_utc - heartbeat).total_seconds()) if heartbeat else None
                stale_threshold = max(60, int(_DB_HEALTH_STALE_AFTER_MINUTES) * 60)

                peer["machine_id"] = str(payload.get("machine_id") or "").strip()
                peer["dataset_signature"] = str(payload.get("dataset_signature") or "").strip()
                peer["pending_unsynced"] = int(payload.get("pending_unsynced") or 0)
                peer["age_seconds"] = age_seconds
                peer["heartbeat_at"] = str(payload.get("heartbeat_at") or "").strip()

                health_token = str(payload.get("local_db_health") or "").strip().lower()
                if health_token == "ok":
                    peer["healthy"] = True
                    peer["status"] = "ok" if (age_seconds is None or age_seconds <= stale_threshold) else "stale"
                    if peer["status"] == "stale":
                        peer["note"] = f"Peer heartbeat is stale ({age_seconds}s old)."
                elif health_token in {"corrupt", "error"}:
                    peer["healthy"] = False
                    peer["status"] = health_token
                    peer["note"] = str(payload.get("local_db_health_error") or "").strip()
                else:
                    peer["status"] = "unknown"
                    peer["note"] = "Peer heartbeat does not include DB health metadata yet."
            else:
                peer["status"] = "waiting"
                peer["note"] = f"No heartbeat file found for peer role '{peer_role}'."
        else:
            peer["status"] = "waiting"
            peer["note"] = "Station status folder is not available yet."

    checked_at = datetime.now(timezone.utc).isoformat()
    overall_ok = bool(local_result.get("healthy") is True)
    if cloud_result.get("configured") and cloud_result.get("exists"):
        overall_ok = overall_ok and bool(cloud_result.get("healthy") is True)

    if station_mode and peer.get("healthy") is False:
        overall_ok = False

    return {
        "checked_at": checked_at,
        "mode": str(mode or "quick_check").strip().lower() or "quick_check",
        "station_mode": station_mode,
        "station_role": role,
        "activation_limit": activation_limit,
        "overall_ok": bool(overall_ok),
        "local": local_result,
        "cloud": cloud_result,
        "peer": peer,
    }


def run_startup_db_auto_heal() -> dict:
    """Run startup DB health checks and attempt safe auto-repair when needed."""
    initial = get_database_health_report(mode="quick_check")
    local_status = initial.get("local", {})

    attempted_actions: list[dict] = []
    repaired = False

    local_bad = local_status.get("healthy") is False or local_status.get("status") in {"corrupt", "error"}
    if local_bad:
        cloud_info = initial.get("cloud", {})
        cloud_path = str(cloud_info.get("path") or "").strip()
        cloud_good = bool(cloud_info.get("healthy") is True)

        if cloud_good and cloud_path:
            restore_result = _restore_db_from_path(
                source_path=cloud_path,
                target_path=DB_PATH,
                reason="startup_auto_heal_from_cloud",
            )
            attempted_actions.append({"kind": "restore_from_cloud", **restore_result})
            repaired = bool(restore_result.get("ok"))
        else:
            emergency_local_backup = _create_health_snapshot(
                DB_PATH,
                source_label="local_corrupt",
                reason="pre_repair_snapshot",
            )
            attempted_actions.append({"kind": "snapshot_local", **emergency_local_backup})
            repaired = False

    final_report = get_database_health_report(mode="quick_check")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "initial": initial,
        "actions": attempted_actions,
        "repaired": bool(repaired),
        "ready": bool(final_report.get("local", {}).get("healthy") is True),
        "final": final_report,
    }


def backup_database_now(*, source: str = "local", label: str = "manual") -> dict:
    """Create an immediate DB backup snapshot.

    source:
        - local: snapshot current machine DB into local backup folder.
        - cloud: snapshot OneDrive DB into local backup folder.
    """
    normalized_source = str(source or "local").strip().lower()
    if normalized_source not in {"local", "cloud"}:
        normalized_source = "local"

    live_cloud_path = _resolve_cloud_provider_and_path(_read_db_config())[1]
    target_path = DB_PATH if normalized_source == "local" else _resolve_gdrive_sync_target(live_cloud_path or "")
    result = _create_health_snapshot(
        target_path,
        source_label=f"{normalized_source}_{label}",
        reason="manual_backup",
    )
    return {
        "source": normalized_source,
        "target_path": target_path,
        **result,
    }


def restore_database_now(*, target: str = "local", source: str = "cloud") -> dict:
    """Restore DB content between local/cloud endpoints.

    Supported combinations:
        - source=cloud,target=local  (pull from OneDrive)
        - source=local,target=cloud  (push local to OneDrive via restore semantics)
    """
    src = str(source or "cloud").strip().lower()
    dst = str(target or "local").strip().lower()

    if src == dst:
        return {
            "ok": False,
            "source": src,
            "target": dst,
            "error": "Source and target must be different.",
            "reason": "manual_restore",
        }

    path_local = DB_PATH
    live_cloud_path = _resolve_cloud_provider_and_path(_read_db_config())[1]
    path_cloud = _resolve_gdrive_sync_target(live_cloud_path or "")

    if not path_cloud and src in ("cloud", "local") and dst in ("cloud", "local") and src != dst:
        return {
            "ok": False,
            "source": src,
            "target": dst,
            "error": "Cloud path is not configured. Please set the cloud backup path in Storage Settings.",
            "reason": "manual_restore",
        }

    if src == "cloud" and dst == "local":
        return _restore_db_from_path(
            source_path=path_cloud,
            target_path=path_local,
            reason="manual_restore_cloud_to_local",
        )

    if src == "local" and dst == "cloud":
        return _restore_db_from_path(
            source_path=path_local,
            target_path=path_cloud,
            reason="manual_restore_local_to_cloud",
        )

    return {
        "ok": False,
        "source": src,
        "target": dst,
        "error": "Unsupported restore direction.",
        "reason": "manual_restore",
    }


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
                student_identifier TEXT DEFAULT '',
                subject TEXT,
                subjects_json TEXT DEFAULT '[]',
                subject_minutes_json TEXT DEFAULT '[]',
                total_study_minutes INTEGER DEFAULT 30,
                book_loaned INTEGER DEFAULT 0,
                email TEXT,
                phone TEXT,
                guardian TEXT DEFAULT '',
                secondary_email TEXT DEFAULT '',
                secondary_phone TEXT DEFAULT '',
                secondary_guardian TEXT DEFAULT '',
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
            "id", "name", "student_identifier", "subject", "subjects_json", "subject_minutes_json", "total_study_minutes",
            "book_loaned", "email", "phone", "guardian", "secondary_email", "secondary_phone", "secondary_guardian", "active", "el", "pi", "v",
            "day1", "day1_time", "day2", "day2_time", "day3", "day3_time", "day4", "day4_time",
            "day5", "day5_time", "day6", "day6_time", "checkout_notify_enabled", "photo_blob",
            "photo_mime", "schedule_json", "qr_code", "device_loaned", "ind",
        ]

        select_exprs = [
            _src("id", "NULL"),
            _src("name", "''"),
            _src("student_identifier", "''"),
            _src("subject", "''"),
            _src("subjects_json", "'[]'"),
            _src("subject_minutes_json", "'[]'"),
            _src("total_study_minutes", "30"),
            _src("book_loaned", "0"),
            _src("email", "''"),
            _src("phone", "''"),
            _src("guardian", "''"),
            _src("secondary_email", "''"),
            _src("secondary_phone", "''"),
            _src("secondary_guardian", "''"),
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


def _snapshot_local_machine_license_fields(db_path: str) -> dict[str, str]:
    """Capture machine-local app_license fields that must survive cloud pulls."""
    if not db_path or not os.path.exists(db_path):
        return {}
    try:
        with sqlite3.connect(db_path) as conn:
            if not _table_exists(conn, "app_license"):
                return {}
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM app_license WHERE id = 1 LIMIT 1").fetchone()
            if not row:
                return {}
            out: dict[str, str] = {}
            if "station_role" in row.keys():
                out["station_role"] = str(row["station_role"] or "").strip()
            if "machine_fingerprint" in row.keys():
                out["machine_fingerprint"] = str(row["machine_fingerprint"] or "").strip()
            return out
    except Exception as exc:
        print(f"[sync] WARNING: failed capturing local machine license fields: {exc}", file=sys.stderr)
        return {}


def _restore_local_machine_license_fields(db_path: str, snapshot: dict[str, str]) -> None:
    """Restore machine-local app_license fields after cloud pull overwrite."""
    if not db_path or not snapshot:
        return
    try:
        with sqlite3.connect(db_path) as conn:
            if not _table_exists(conn, "app_license"):
                return

            has_station_role = _column_exists(conn, "app_license", "station_role")
            has_machine_fingerprint = _column_exists(conn, "app_license", "machine_fingerprint")
            has_updated_at = _column_exists(conn, "app_license", "updated_at")

            assignments = []
            params: list[str] = []

            if has_station_role and "station_role" in snapshot:
                assignments.append("station_role = ?")
                params.append(str(snapshot.get("station_role") or "").strip())

            if has_machine_fingerprint and "machine_fingerprint" in snapshot:
                assignments.append("machine_fingerprint = ?")
                params.append(str(snapshot.get("machine_fingerprint") or "").strip())

            if not assignments:
                return

            if has_updated_at:
                assignments.append("updated_at = ?")
                params.append(datetime.now(timezone.utc).isoformat())

            row = conn.execute("SELECT id FROM app_license WHERE id = 1 LIMIT 1").fetchone()
            if row:
                params.append(1)
                conn.execute(
                    f"UPDATE app_license SET {', '.join(assignments)} WHERE id = ?",
                    params,
                )
            else:
                insert_cols = ["id"]
                insert_vals: list[str | int] = [1]
                if has_station_role and "station_role" in snapshot:
                    insert_cols.append("station_role")
                    insert_vals.append(str(snapshot.get("station_role") or "").strip())
                if has_machine_fingerprint and "machine_fingerprint" in snapshot:
                    insert_cols.append("machine_fingerprint")
                    insert_vals.append(str(snapshot.get("machine_fingerprint") or "").strip())
                if has_updated_at:
                    insert_cols.append("updated_at")
                    insert_vals.append(datetime.now(timezone.utc).isoformat())
                placeholders = ", ".join(["?" for _ in insert_cols])
                conn.execute(
                    f"INSERT INTO app_license ({', '.join(insert_cols)}) VALUES ({placeholders})",
                    insert_vals,
                )

            conn.commit()
    except Exception as exc:
        print(f"[sync] WARNING: failed restoring local machine license fields: {exc}", file=sys.stderr)


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
    local_machine_license_snapshot = _snapshot_local_machine_license_fields(local_path)
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
            _restore_local_machine_license_fields(local_path, local_machine_license_snapshot)
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
                    _record_sync_failure()
                    return False

            _sqlite_backup(local_path, gdrive_path)
            if not silent:
                summary = _db_summary(local_path)
                print(
                    f"[sync] Pushed DB to {cloud_name}: {gdrive_path}\n"
                    f"[sync] Snapshot -> {summary}"
                )
            _set_last_sync_error("")
            _reset_sync_retry_state()  # Reset on successful sync
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
                _record_sync_failure()
                return False
        except Exception as exc:
            if not silent:
                print(f"[sync] WARNING: push to GDrive failed: {exc}", file=sys.stderr)
            _set_last_sync_error(str(exc))
            _record_sync_failure()
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
    """Daemon thread that pushes local → GDrive with adaptive retry on failures.
    
    Normal cycle: every interval_minutes (9 min).
    On failure: exponential backoff (45s, 90s, 180s), then wait until next 9-min cycle.
    On success: retry state resets.
    """
    if interval_minutes <= 0 or not gdrive_path:
        return

    def _loop():
        cycle_duration = max(60, int(interval_minutes) * 60)
        next_regular_at = time.time() + cycle_duration

        while True:
            now = time.time()
            should_attempt = False

            if now >= next_regular_at:
                should_attempt = True
            elif _should_attempt_sync_now(now):
                status = get_sync_retry_status()
                should_attempt = int(status.get("failure_count") or 0) > 0

            if should_attempt:
                pushed = sync_to_gdrive(local_path, gdrive_path, silent=True)
                if pushed:
                    next_regular_at = time.time() + cycle_duration
                else:
                    status = get_sync_retry_status()
                    if int(status.get("next_retry_seconds") or 0) == -1:
                        next_regular_at = time.time() + cycle_duration

            time.sleep(1)

    t = threading.Thread(target=_loop, daemon=True, name="gdrive-sync")
    t.start()
    print(f"[sync] Background sync thread started (every {interval_minutes} min -> {gdrive_path})")
    print(f"[sync] Adaptive retry delays: {_SYNC_RETRY_DELAYS} seconds")


def _start_snapshot_backup_timer(interval_minutes: int = _SNAPSHOT_INTERVAL_MINUTES_DEFAULT) -> None:
    """Run periodic local snapshot backups on Instructor server mode."""
    global _SNAPSHOT_TIMER_THREAD

    if _SNAPSHOT_TIMER_THREAD and _SNAPSHOT_TIMER_THREAD.is_alive():
        return

    wait_seconds = max(300, int(interval_minutes) * 60)
    _SNAPSHOT_TIMER_STOP_EVENT.clear()

    def _loop() -> None:
        while not _SNAPSHOT_TIMER_STOP_EVENT.wait(wait_seconds):
            try:
                result = backup_database_now(source="local", label="auto")
                if result.get("ok"):
                    print(f"[backup] Auto snapshot created: {result.get('path')}")
                else:
                    print(
                        f"[backup] WARNING: auto snapshot failed: {result.get('error') or 'unknown error'}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(f"[backup] WARNING: auto snapshot loop error: {exc}", file=sys.stderr)

    _SNAPSHOT_TIMER_THREAD = threading.Thread(
        target=_loop,
        daemon=True,
        name="instructor-snapshot-backup",
    )
    _SNAPSHOT_TIMER_THREAD.start()
    print(f"[backup] Instructor snapshot timer started (every {wait_seconds // 60} min)")


def stop_snapshot_backup_timer() -> None:
    _SNAPSHOT_TIMER_STOP_EVENT.set()


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
    if _BACKUP_MODE == "instructor_snapshots_only":
        return True

    if is_station_mailbox_mode_enabled():
        return True

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
_RUNTIME_CFG = _station_runtime_config_from_cfg(_cfg)
_STATION_MODE = _RUNTIME_CFG["station_mode"]
_BACKUP_MODE = _RUNTIME_CFG["backup_mode"]
_SNAPSHOT_INTERVAL_MINUTES = _RUNTIME_CFG["snapshot_interval_minutes"]

DB_PATH = _resolve_db_path()

def _initialize_cloud_sync_runtime() -> None:
    """Start cloud sync runtime without holding the cloud lease open."""
    global _CLOUD_SYNC_RUNTIME_READY
    if _CLOUD_SYNC_RUNTIME_READY:
        return

    if _BACKUP_MODE == "instructor_snapshots_only":
        if _STATION_MODE == "instructor_server":
            _start_snapshot_backup_timer(_SNAPSHOT_INTERVAL_MINUTES)
        _CLOUD_SYNC_RUNTIME_READY = True
        print("[backup] Instructor snapshots-only mode enabled (cloud live-sync disabled).")
        return

    if not GDRIVE_SYNC_PATH:
        print("[sync] Startup override skipped: cloud backup path is not configured.")
        _CLOUD_SYNC_RUNTIME_READY = True
        return

    startup_pull_from_gdrive = bool(_cfg.get("startup_pull_from_gdrive", False))
    if is_station_mailbox_mode_enabled():
        # Scanner stations that have an empty local database (first setup) should seed
        # themselves from the OneDrive backup before starting the mailbox sync.
        # This avoids a long wait for the first Admin_Outbox export cycle to deliver data.
        _, _role = _read_station_sync_state()
        if _role == "checkin":
            try:
                with sqlite3.connect(DB_PATH) as _seed_conn:
                    _student_count = _seed_conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
            except Exception:
                _student_count = 0
            if _student_count == 0:
                print("[mailbox-sync] Scanner station local DB is empty; pulling OneDrive backup to seed data.")
                pulled = sync_from_gdrive(DB_PATH, GDRIVE_SYNC_PATH, force=True)
                if pulled:
                    print("[mailbox-sync] Scanner station successfully seeded from OneDrive backup.")
                else:
                    print(
                        f"[mailbox-sync] Scanner station seed pull skipped: "
                        f"{_LAST_SYNC_ERROR or 'OneDrive backup not available yet.'}"
                    )
        _start_station_mailbox_sync(DB_PATH, GDRIVE_SYNC_PATH, _MAILBOX_SYNC_INTERVAL_MINUTES)
        _CLOUD_SYNC_RUNTIME_READY = True
        print("[mailbox-sync] Multi-station Inbox/Outbox mode enabled.")
        return

    if startup_pull_from_gdrive:
        pulled = sync_from_gdrive(DB_PATH, GDRIVE_SYNC_PATH, force=True)
        if not pulled:
            print(f"[sync] Startup override skipped: {_cloud_label(GDRIVE_SYNC_PATH)} DB not found/unavailable.")

    _start_background_sync(DB_PATH, GDRIVE_SYNC_PATH, _SYNC_INTERVAL)
    _start_onedrive_folder_monitor(GDRIVE_SYNC_PATH)
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
    stop_station_mailbox_sync()
    stop_onedrive_folder_monitor()
    stop_manual_checkpoint_timer()
    stop_snapshot_backup_timer()
    restore_onedrive_quarantined_files()


atexit.register(_sync_on_exit)

def init_db():
    db_parent = os.path.dirname(DB_PATH)
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)

    startup_heal = run_startup_db_auto_heal()
    if startup_heal.get("repaired"):
        print("[db-health] Startup auto-heal restored local DB from a healthy source.")
    if not startup_heal.get("ready"):
        final_local = startup_heal.get("final", {}).get("local", {})
        raise RuntimeError(
            "Local database failed health checks and could not be auto-repaired: "
            f"{final_local.get('error') or final_local.get('result') or final_local.get('status') or 'unknown error'}"
        )

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
            last_name TEXT DEFAULT '',
            student_identifier TEXT DEFAULT '',
            subject TEXT,
            subjects_json TEXT DEFAULT '[]',
            subject_minutes_json TEXT DEFAULT '[]',
            total_study_minutes INTEGER DEFAULT 30,
            book_loaned INTEGER DEFAULT 0,
            email TEXT,
            phone TEXT,
            guardian TEXT DEFAULT '',
            secondary_email TEXT DEFAULT '',
            secondary_phone TEXT DEFAULT '',
            secondary_guardian TEXT DEFAULT '',
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

    # Additive migration for Excel student imports; existing data is preserved.
    student_columns = {row[1] for row in c.execute("PRAGMA table_info(students)").fetchall()}
    if "last_name" not in student_columns:
        c.execute("ALTER TABLE students ADD COLUMN last_name TEXT DEFAULT ''")

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
            borrower_id INTEGER,
            cover_blob BLOB,
            cover_mime TEXT DEFAULT '',
            cover_lookup_attempted INTEGER DEFAULT 0
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
            sync_synced INTEGER DEFAULT 0,
            sync_source_machine TEXT DEFAULT '',
            sync_source_row_id INTEGER,
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
            sync_synced INTEGER DEFAULT 0,
            sync_source_machine TEXT DEFAULT '',
            sync_source_row_id INTEGER,
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

    # Cancellation notices (customer acknowledgement workflow)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cancellation_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_ids_json TEXT NOT NULL DEFAULT '[]',
            student_names_json TEXT NOT NULL DEFAULT '[]',
            selection_label TEXT DEFAULT '',
            notice_date TEXT NOT NULL,
            effective_last_attendance_date TEXT NOT NULL,
            customer_name TEXT NOT NULL DEFAULT '',
            customer_email TEXT DEFAULT '',
            ack_method TEXT NOT NULL DEFAULT 'checkbox',
            ack_identity_confirmed INTEGER NOT NULL DEFAULT 0,
            ack_last_day_confirmed INTEGER NOT NULL DEFAULT 0,
            center_email_sent INTEGER NOT NULL DEFAULT 0,
            center_email_status TEXT DEFAULT '',
            center_email_sent_at TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
    _ensure_qr_registry_table(conn)

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

    # Keep main DB file freshness predictable under WAL mode.
    _start_manual_checkpoint_timer(_CHECKPOINT_INTERVAL_SECONDS)

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
        if "secondary_email" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN secondary_email TEXT DEFAULT ''")
        if "secondary_phone" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN secondary_phone TEXT DEFAULT ''")
        if "secondary_guardian" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN secondary_guardian TEXT DEFAULT ''")
        if "qr_code" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN qr_code BLOB")
        if "device_loaned" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN device_loaned INTEGER DEFAULT 0")
        if "ind" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN ind INTEGER DEFAULT 0")
        if "checkout_notify_enabled" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN checkout_notify_enabled INTEGER DEFAULT 1")
        if "student_identifier" not in cols:
            cur.execute("ALTER TABLE students ADD COLUMN student_identifier TEXT DEFAULT ''")
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
        if "cover_blob" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN cover_blob BLOB")
        if "cover_mime" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN cover_mime TEXT DEFAULT ''")
        if "cover_lookup_attempted" not in cols:
            cur.execute("ALTER TABLE books ADD COLUMN cover_lookup_attempted INTEGER DEFAULT 0")
        
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
        if "sync_synced" not in cols:
            cur.execute("ALTER TABLE sessions ADD COLUMN sync_synced INTEGER DEFAULT 0")
        if "sync_source_machine" not in cols:
            cur.execute("ALTER TABLE sessions ADD COLUMN sync_source_machine TEXT DEFAULT ''")
        if "sync_source_row_id" not in cols:
            cur.execute("ALTER TABLE sessions ADD COLUMN sync_source_row_id INTEGER")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_sync_synced ON sessions(sync_synced)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_sync_source ON sessions(sync_source_machine, sync_source_row_id)")

        cur.execute("PRAGMA table_info(assistant_sessions)")
        cols = [r[1] for r in cur.fetchall()]
        if "sync_synced" not in cols:
            cur.execute("ALTER TABLE assistant_sessions ADD COLUMN sync_synced INTEGER DEFAULT 0")
        if "sync_source_machine" not in cols:
            cur.execute("ALTER TABLE assistant_sessions ADD COLUMN sync_source_machine TEXT DEFAULT ''")
        if "sync_source_row_id" not in cols:
            cur.execute("ALTER TABLE assistant_sessions ADD COLUMN sync_source_row_id INTEGER")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_assistant_sessions_sync_synced ON assistant_sessions(sync_synced)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_assistant_sessions_sync_source ON assistant_sessions(sync_source_machine, sync_source_row_id)")
        conn.commit()

    # Ensure assistant_schedule table exists
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(assistant_schedule)")
        cols = [r[1] for r in cur.fetchall()]
        conn.commit()

    # Backfill all existing QR values into global registry (including inactive students).
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        _ensure_qr_registry_table(conn)

        student_qr_rows = cur.execute(
            "SELECT id, qr_code FROM students WHERE qr_code IS NOT NULL",
            (),
        ).fetchall()
        for sid, qr_blob in student_qr_rows:
            if qr_blob is None:
                continue
            if isinstance(qr_blob, memoryview):
                qr_blob = qr_blob.tobytes()
            # Student QR data is stored as image blob; track deterministic ID token as retired sentinel.
            register_qr_token(f"ID:{int(sid)}", "student", int(sid), retired=1)

        material_qr_rows = cur.execute(
            "SELECT id, qr_code FROM materials WHERE COALESCE(TRIM(qr_code), '') <> ''",
            (),
        ).fetchall()
        for mid, qr_value in material_qr_rows:
            register_qr_token(str(qr_value).strip(), "material", int(mid), retired=0)

        assistant_rows = cur.execute(
            "SELECT id FROM staff",
            (),
        ).fetchall()
        for row in assistant_rows:
            register_qr_token(f"ASST:{int(row[0])}", "assistant", int(row[0]), retired=1)

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

    # Ensure cancellation_notices table and columns exist
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cancellation_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_ids_json TEXT NOT NULL DEFAULT '[]',
                student_names_json TEXT NOT NULL DEFAULT '[]',
                selection_label TEXT DEFAULT '',
                notice_date TEXT NOT NULL,
                effective_last_attendance_date TEXT NOT NULL,
                customer_name TEXT NOT NULL DEFAULT '',
                customer_email TEXT DEFAULT '',
                ack_method TEXT NOT NULL DEFAULT 'checkbox',
                ack_identity_confirmed INTEGER NOT NULL DEFAULT 0,
                ack_last_day_confirmed INTEGER NOT NULL DEFAULT 0,
                center_email_sent INTEGER NOT NULL DEFAULT 0,
                center_email_status TEXT DEFAULT '',
                center_email_sent_at TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("PRAGMA table_info(cancellation_notices)")
        cols = [r[1] for r in cur.fetchall()]

        if "student_ids_json" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN student_ids_json TEXT NOT NULL DEFAULT '[]'")
        if "student_names_json" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN student_names_json TEXT NOT NULL DEFAULT '[]'")
        if "selection_label" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN selection_label TEXT DEFAULT ''")
        if "notice_date" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN notice_date TEXT NOT NULL DEFAULT ''")
        if "effective_last_attendance_date" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN effective_last_attendance_date TEXT NOT NULL DEFAULT ''")
        if "customer_name" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
        if "customer_email" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN customer_email TEXT DEFAULT ''")
        if "ack_method" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN ack_method TEXT NOT NULL DEFAULT 'checkbox'")
        if "ack_identity_confirmed" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN ack_identity_confirmed INTEGER NOT NULL DEFAULT 0")
        if "ack_last_day_confirmed" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN ack_last_day_confirmed INTEGER NOT NULL DEFAULT 0")
        if "center_email_sent" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN center_email_sent INTEGER NOT NULL DEFAULT 0")
        if "center_email_status" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN center_email_status TEXT DEFAULT ''")
        if "center_email_sent_at" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN center_email_sent_at TEXT DEFAULT ''")
        if "created_at" not in cols:
            cur.execute("ALTER TABLE cancellation_notices ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP")

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