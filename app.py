# Stdytime v2.3.12 - Main Flask Application (Refactored)
# ================================================================
"""
Stdytime: Student class management system with dashboard, QR codes, and PDF label generation.
Features: Student management, session tracking, QR generation, Avery 8160 PDF output, staff duty tracking.
"""

from flask import Flask, render_template, request, send_from_directory, jsonify, session, g, redirect, url_for
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import sqlite3
import os
import secrets
import sys
import shutil
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect, generate_csrf
import logging
import atexit

# Load environment variables from .env file
load_dotenv()

from modules.database import (
    init_db,
    DB_PATH,
    GDriveLockError,
    get_db_config_status,
    sync_to_gdrive_now,
    record_app_version,
    get_version_compatibility_warning,
)
from modules import student_manager, timer_manager, qr_generator, assistant_manager, reports, auth_manager, license_manager
from modules import instructor_profile_manager
from modules import user_identity_manager
from modules import server_cache
from modules.utils import format_hhmm
from modules.rate_limiter import limiter
from modules.single_instance import ensure_single_instance, release_single_instance_lock


def _should_enforce_single_instance() -> bool:
    """Avoid lock contention with Flask's reloader parent process in development."""
    use_reloader = os.getenv("FLASK_USE_RELOADER", "true").lower() == "true"
    if use_reloader and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return False
    return True


def _acquire_single_instance_or_exit() -> bool:
    """Acquire single-instance guard for server startup.

    Returns:
        bool: True when startup may continue, False when blocked in a debugger
        session (no SystemExit raised).
    """
    if not _should_enforce_single_instance():
        return True
    try:
        ensure_single_instance(
            app_name='stdytime-app',
            host=os.getenv('HOST', '127.0.0.1'),
            port=int(os.getenv('PORT', '5000')),
        )
        atexit.register(release_single_instance_lock)
        return True
    except RuntimeError as exc:
        if _is_debugger_attached():
            print(
                f"[startup] BLOCKED (debugger): {exc} "
                "A server instance is already running; ending this debug run without SystemExit.",
                file=sys.stderr,
            )
        else:
            print(f"[startup] BLOCKED: {exc}", file=sys.stderr)
        return False

# ================================================================
#  Flask setup
# ================================================================
app = Flask(__name__)
app.config['DEV_TRACE_ENABLED'] = False
app.config['DEV_TRACE_FILE_PATH'] = ''

IS_PRODUCTION = (
    os.getenv('APP_ENV', 'development').lower() == 'production'
    or os.getenv('RENDER', '').lower() == 'true'
)

_DEV_TRACE_HANDLE = None
_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr
_EXIT_SHUTDOWN_IN_PROGRESS = False


def _is_debugger_attached():
    """Return True when the current process is running under a debugger."""
    # Common debugpy markers in VS Code launches.
    if os.getenv("DEBUGPY_LAUNCHER_PORT") or os.getenv("PYDEVD_LOAD_VALUES_ASYNC"):
        return True

    gettrace = getattr(sys, 'gettrace', None)
    if callable(gettrace) and gettrace():
        return True

    # Fallback: debugpy may be imported before trace is fully active.
    return 'debugpy' in sys.modules


class _TeeStream:
    """Write stream output to both the original stream and a trace file."""

    def __init__(self, primary_stream, trace_stream):
        self._primary = primary_stream
        self._trace = trace_stream

    @property
    def encoding(self):
        return getattr(self._primary, "encoding", "utf-8")

    def write(self, data):
        self._primary.write(data)
        self._trace.write(data)
        return len(data)

    def flush(self):
        self._primary.flush()
        self._trace.flush()

    def isatty(self):
        return bool(getattr(self._primary, "isatty", lambda: False)())


def _setup_development_trace(flask_app):
    """Enable per-launch trace logging in development mode only."""
    global _DEV_TRACE_HANDLE

    if IS_PRODUCTION:
        return

    if os.getenv('DEV_TRACE_ENABLED', 'true').lower() != 'true':
        return

    use_reloader = os.getenv("FLASK_USE_RELOADER", "true").lower() == "true"
    if use_reloader and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        # Skip the watchdog parent process; enable trace only for the active worker.
        return

    trace_dir = os.path.join(os.getcwd(), 'trace_logs')
    os.makedirs(trace_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    trace_path = os.path.join(trace_dir, f'trace_{stamp}_{os.getpid()}.txt')

    _DEV_TRACE_HANDLE = open(trace_path, 'a', encoding='utf-8', buffering=1)
    sys.stdout = _TeeStream(_ORIGINAL_STDOUT, _DEV_TRACE_HANDLE)
    sys.stderr = _TeeStream(_ORIGINAL_STDERR, _DEV_TRACE_HANDLE)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    logging.getLogger('werkzeug').setLevel(logging.DEBUG)
    flask_app.logger.setLevel(logging.DEBUG)

    flask_app.config['DEV_TRACE_ENABLED'] = True
    flask_app.config['DEV_TRACE_FILE_PATH'] = trace_path
    print(f"[trace] Development trace enabled: {trace_path}")


def _close_development_trace():
    global _DEV_TRACE_HANDLE
    if isinstance(sys.stdout, _TeeStream):
        sys.stdout = _ORIGINAL_STDOUT
    if isinstance(sys.stderr, _TeeStream):
        sys.stderr = _ORIGINAL_STDERR
    if _DEV_TRACE_HANDLE:
        try:
            _DEV_TRACE_HANDLE.flush()
            _DEV_TRACE_HANDLE.close()
        except Exception:
            pass
        _DEV_TRACE_HANDLE = None


_setup_development_trace(app)
atexit.register(_close_development_trace)

_raw_secret = os.getenv('SECRET_KEY')
if not _raw_secret:
    _raw_secret = secrets.token_hex(32)
    print(
        "WARNING: SECRET_KEY not set in environment. "
        "Sessions will not persist across restarts. "
        "Set SECRET_KEY in your .env file.",
        file=sys.stderr,
    )
app.secret_key = _raw_secret

# Session configuration
app.config['SESSION_TYPE'] = 'filesystem'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
cookie_secure_default = 'true' if IS_PRODUCTION else 'false'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('COOKIE_SECURE', cookie_secure_default).lower() == 'true'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_TIME_LIMIT'] = 3600  # CSRF token valid for 1 hour

# Initialize / verify sqlite DB
try:
    init_db()
    print(f"[startup] Database initialized at: {DB_PATH}")
except GDriveLockError as lock_err:
    print(
        f"\n[startup] BLOCKED: {lock_err}\n"
        "StdyTime cannot start while another machine is using the database.\n"
        "Close the app on the other machine, then restart here.",
        file=sys.stderr,
    )
    sys.exit(1)
except Exception as db_init_error:
    print(
        f"[startup] FATAL: Database initialization failed for DB_PATH='{DB_PATH}': {db_init_error}",
        file=sys.stderr,
    )
    raise

# Security extensions
csrf = CSRFProtect(app)
limiter.init_app(app)

# Cleanup old payroll data (18 month retention policy)
assistant_manager.cleanup_old_payroll_data(months=18)

# Enable cache traces in the terminal for dashboard/column-3 debugging.
server_cache.DEBUG_CACHE = os.getenv('DEBUG_CACHE', 'false').lower() == 'true'
server_cache._logger.setLevel(logging.DEBUG)
if not server_cache._logger.handlers:
    _cache_handler = logging.StreamHandler()
    _cache_handler.setFormatter(logging.Formatter('%(message)s'))
    server_cache._logger.addHandler(_cache_handler)
server_cache._logger.propagate = False

# ================================================================
#  Request Profiling - Track reads and writes
# ================================================================
class RequestProfiler:
    """Track HTTP read (GET) and write (POST/PUT/DELETE/PATCH) operations."""
    def __init__(self):
        self.total_reads = 0
        self.total_writes = 0
        self.endpoint_stats = {}
        self.log_file = os.path.join(os.getcwd(), 'request_profile.log')
    
    def log_request(self, method, endpoint, status_code):
        """Log a request and update statistics."""
        is_write = method in ('POST', 'PUT', 'DELETE', 'PATCH')
        
        if is_write:
            self.total_writes += 1
        else:
            self.total_reads += 1
        
        # Track by endpoint
        key = f"{method} {endpoint}"
        if key not in self.endpoint_stats:
            self.endpoint_stats[key] = {'count': 0, 'statuses': []}
        self.endpoint_stats[key]['count'] += 1
        if status_code not in self.endpoint_stats[key]['statuses']:
            self.endpoint_stats[key]['statuses'].append(status_code)
        
        # Console output (compact format)
        req_type = "WRITE" if is_write else "READ"
        print(f"[{req_type}] {method:6} {endpoint:40} {status_code}")
        
        # Log to file
        self._write_log(f"[{req_type}] {method:6} {endpoint:40} {status_code}")
    
    def _write_log(self, message):
        """Append to log file."""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {message}\n")
        except Exception:
            pass
    
    def print_summary(self):
        """Print summary of requests."""
        total = self.total_reads + self.total_writes
        if total == 0:
            return
        
        summary = (
            f"\n{'='*80}\n"
            f"REQUEST PROFILE SUMMARY\n"
            f"{'='*80}\n"
            f"Total READs  (GET):                    {self.total_reads}\n"
            f"Total WRITEs (POST/PUT/DELETE):        {self.total_writes}\n"
            f"Total Requests:                        {total}\n"
            f"{'='*80}\n"
        )
        print(summary)
        self._write_log(summary)
        
        # Endpoint breakdown
        if self.endpoint_stats:
            print("\nEndpoint Breakdown:")
            for endpoint, stats in sorted(self.endpoint_stats.items(), key=lambda x: x[1]['count'], reverse=True):
                print(f"  {endpoint:50} {stats['count']:3} requests (Status: {', '.join(map(str, stats['statuses']))})")

profiler = RequestProfiler()

@app.before_request
def before_request_license_state():
    """Load license state via LemonSqueezy (with local cache) and block access when unlicensed."""
    # DEV_LICENSE_BYPASS=true skips all license enforcement for local development.
    if os.getenv("DEV_LICENSE_BYPASS", "").strip().lower() == "true":
        g.license_status = {
            "is_valid": True,
            "status": "valid",
            "licensee": "Developer",
            "email": "dev@localhost",
            "expires_at": "2099-12-31",
            "issued_at": "",
            "days_remaining": None,
            "machine_fingerprint": "*",
            "machine_name": "dev",
            "has_license_key": True,
            "message": "Development bypass active.",
            "default_home_endpoint": "dashboard",
        }
        g.current_user = license_manager.get_local_user(g.license_status)
        return None

    from modules import ls_license as _ls
    ls_ctx = _ls.get_ls_license_context()
    # Prefer LS context if an instance has been activated; fall back to local HMAC
    if ls_ctx.get("has_license_key"):
        force_revalidate = _ls.should_force_revalidate_request(
            method=request.method,
            path=request.path,
            endpoint=request.endpoint,
        )
        _ls.verify_ls_license(force=force_revalidate)
        ls_ctx = _ls.get_ls_license_context()
        g.license_status = ls_ctx
    else:
        g.license_status = license_manager.get_license_context()
    g.current_user = license_manager.get_local_user(g.license_status)

    allowed_endpoints = {
        'static',
        'license_page',
        'activate_license',
        'remove_license',
        'verify_license',
        'license_expired',
        'license_status_api',
        'lemonsqueezy_webhook',
        'api_csrf_token',
        'healthz',
        'not_found',
    }

    if request.endpoint in allowed_endpoints or request.path.startswith('/static/'):
        return None

    if g.license_status.get('is_valid'):
        return None

    if request.path.startswith('/api/'):
        return jsonify({
            'error': g.license_status.get('message', 'A valid license is required.'),
            'license_status': g.license_status.get('status', 'unlicensed'),
            'license_page': url_for('license_page'),
        }), 403

    target = 'license_expired' if g.license_status.get('status') == 'expired' else 'license_page'
    return redirect(url_for(target))

@app.before_request
def before_request_profiler():
    """Capture request start time."""
    g.start_time = datetime.now()
    app.logger.debug("[request] -> %s %s", request.method, request.path)


@app.before_request
def before_request_capture_email():
    """Require a startup email once, then reuse it across sessions."""
    # Do not gate unlicensed flows; license middleware handles those.
    if not (g.get('license_status') or {}).get('is_valid'):
        return None

    allowed_endpoints = {
        'static',
        'license_page',
        'activate_license',
        'remove_license',
        'verify_license',
        'license_expired',
        'license_status_api',
        'lemonsqueezy_webhook',
        'api_csrf_token',
        'healthz',
        'email_login',
        'setup_requirements',
        'setup_storage',
        'api_setup_status',
        'instructor_profile_edit',
        'instructor_profile',
        'not_found',
    }

    if request.endpoint in allowed_endpoints or request.path.startswith('/static/'):
        return None

    # Enforce persisted identity: app should not continue on transient session-only email.
    active_email = user_identity_manager.get_saved_email()
    if not active_email:
        active_email = user_identity_manager.resolve_active_email(None)
    if active_email:
        # Cross-check the saved email against the LemonSqueezy license email.
        from modules import ls_license as _ls_mod
        mismatch = _ls_mod.validate_email_matches_license(active_email)
        if mismatch:
            # Saved email no longer matches the license — force re-entry.
            user_identity_manager.clear_saved_email()
            session.pop('user_email', None)
            if request.path.startswith('/api/'):
                return jsonify({'error': mismatch, 'setup_url': url_for('email_login')}), 428
            return redirect(url_for('email_login', next=request.path))
        session['user_email'] = active_email
        session.permanent = True
        user_identity_manager.sync_instructor_profile_email(active_email)
        return None

    if request.path.startswith('/api/'):
        return jsonify({
            'error': 'Email setup required before using the API.',
            'setup_url': url_for('email_login'),
        }), 428

    next_url = request.full_path if request.query_string else request.path
    return redirect(url_for('email_login', next=next_url))


def _has_configured_instructor_hours() -> bool:
    profile = instructor_profile_manager.get_instructor_profile()
    if not profile:
        return False
    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    return any(
        (profile.get(f'{day}_start') or '').strip() and (profile.get(f'{day}_end') or '').strip()
        for day in days
    )


@app.before_request
def before_request_enforce_first_run_setup():
    """Hard-stop all app flows until storage config and class-hours setup are complete."""
    if not (g.get('license_status') or {}).get('is_valid'):
        return None

    setup_allowed_endpoints = {
        'static',
        'email_login',
        'setup_requirements',
        'setup_storage',
        'instructor_profile_edit',
        'instructor_profile',
        'api_setup_status',
        'api_csrf_token',
        'healthz',
        'not_found',
    }

    if request.endpoint in setup_allowed_endpoints or request.path.startswith('/static/'):
        return None

    storage_ready = bool(get_db_config_status().get('is_ready'))
    profile_ready = _has_configured_instructor_hours()

    if storage_ready and profile_ready:
        return None

    if request.path.startswith('/api/'):
        return jsonify({
            'error': 'First-time setup is required before using the API.',
            'setup_url': url_for('setup_requirements'),
            'requirements': {
                'storage_ready': storage_ready,
                'profile_hours_ready': profile_ready,
            },
        }), 428

    return redirect(url_for('setup_requirements'))

@app.after_request
def after_request_profiler(response):
    """Log request after it completes."""
    from flask import g
    endpoint = request.path
    profiler.log_request(request.method, endpoint, response.status_code)
    app.logger.debug("[request] <- %s %s %s", request.method, endpoint, response.status_code)
    return response


@app.after_request
def after_request_immediate_backup_sync(response):
    """Immediately push local DB to cloud backup after successful write operations."""
    try:
        is_write = request.method in ('POST', 'PUT', 'PATCH', 'DELETE')
        is_success = 200 <= int(response.status_code) < 400
        if is_write and is_success:
            sync_to_gdrive_now()
    except Exception as exc:
        app.logger.warning("[sync] Immediate post-write backup push failed: %s", exc)
    return response

# Prevent client/proxies from caching API responses
@app.after_request
def add_no_cache_headers(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# Folders for CSV IO
UPLOAD_FOLDER = "uploads"
EXPORT_FOLDER = "exports"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXPORT_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# ================================================================
#  Request Profiling - Track reads and writes
# ================================================================
@app.context_processor
def inject_now():
    """Inject current date/time into all templates."""
    now = datetime.now()
    return dict(
        date_str=now.strftime("%A, %B %d, %Y"),
        time_str=now.strftime("%I:%M:%S %p"),
    )


@app.context_processor
def inject_current_user():
    """Inject the single local licensed operator into all templates."""
    return dict(
        current_user=g.get('current_user'),
        user_session={'email': session.get('user_email', '')}
    )


@app.context_processor
def inject_subscription_access():
    """Inject navigation access flags."""
    license_status = g.get('license_status', {})
    context = {
        'can_access_students': True,
        'can_access_books': True,
        'can_access_assistants': True,
        'can_access_stdytimeclass': True,
        'can_access_utilities_print': True,
        'can_send_email': True,
        'can_access_instructor_profile': True,
        'can_access_instructor_reports': True,
        'can_access_instructor_settings': True,
        'can_access_qr': True,
        'default_home_endpoint': 'dashboard',
    }
    context['is_licensed'] = bool(license_status.get('is_valid'))
    context['license_status'] = license_status.get('status', 'unlicensed')
    return dict(subscription_access=context)


@app.context_processor
def inject_license_status():
    """Expose local license metadata to templates."""
    status = g.get('license_status', license_manager.get_license_context())
    nav_badge = {
        'visible': False,
        'tone': 'secondary',
        'label': 'LS',
        'age_text': 'n/a',
        'cache_remaining_text': 'n/a',
        'cache_remaining_pct': 0,
        'grace_mode': False,
        'tooltip': 'License telemetry unavailable.',
    }
    try:
        from modules import ls_license as _ls
        nav_badge = _ls.get_nav_badge_data()
    except Exception:
        pass
    return dict(license_status=status, ls_nav_badge=nav_badge)


@app.context_processor
def inject_app_version():
    """Inject app version from VERSION file into all templates."""
    return dict(app_version=get_app_version())


@app.context_processor
def inject_app_version_compatibility():
    """Inject cross-machine app/backup version compatibility banner state."""
    current_version = get_app_version()
    return dict(version_compatibility=get_version_compatibility_warning(current_version))


@app.context_processor
def inject_branding():
    """Inject profile-based branding and shared theme values into templates."""
    try:
        from modules.email_manager import resolve_center_name

        profile = instructor_profile_manager.get_instructor_profile()
        center_name = resolve_center_name()
    except Exception:
        profile = None
        center_name = 'Stdytime Center'
    return dict(
        branding_profile=profile,
        branding_center_name=center_name,
        brand_primary='#2e7d32',
        brand_primary_dark='#1b5e20',
        brand_accent='#fdd835',
    )


@app.context_processor
def inject_dev_trace_context():
    """Expose development trace metadata to templates."""
    trace_path = app.config.get('DEV_TRACE_FILE_PATH', '') or ''
    return dict(
        dev_trace_enabled=bool(app.config.get('DEV_TRACE_ENABLED', False)),
        dev_trace_filename=os.path.basename(trace_path) if trace_path else '',
    )


def _bump_patch_version(version):
    """Bump version with rollover: x.x.(x+1) where each segment goes 0-99.
    Example: 06.07.59 → 06.07.60, 06.07.99 → 06.08.00, 06.99.99 → 07.00.00
    """
    parts = version.split('.')
    if len(parts) < 3:
        return version
    
    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        width = [len(parts[0]), len(parts[1]), len(parts[2])]
        
        # Increment patch
        patch += 1
        
        # Rollover logic
        if patch > 99:
            patch = 0
            minor += 1
            if minor > 99:
                minor = 0
                major += 1
        
        return f"{str(major).zfill(width[0])}.{str(minor).zfill(width[1])}.{str(patch).zfill(width[2])}"
    except (ValueError, IndexError):
        return version


def _find_latest_source_mtime(base_dir):
    ignore_dirs = {
        '.venv', '__pycache__', '.git', 'build', 'dist', 'data',
        'exports', 'uploads', 'assets', 'static', 'templates\\static'
    }
    latest = 0.0
    for root, dirs, files in os.walk(base_dir):
        rel_root = os.path.relpath(root, base_dir)
        if rel_root == '.':
            rel_root = ''
        rel_parts = rel_root.split(os.sep) if rel_root else []
        if any(part in ignore_dirs for part in rel_parts):
            dirs[:] = []
            continue
        for fname in files:
            if fname == 'VERSION':
                continue
            fpath = os.path.join(root, fname)
            try:
                mtime = os.path.getmtime(fpath)
                if mtime > latest:
                    latest = mtime
            except Exception:
                continue
    return latest



def _ensure_version_up_to_date():
    """Always bump version in VERSION file on every app start."""
    vpath = os.path.join(os.path.dirname(__file__), 'VERSION')
    lock_path = vpath + '.startup-bump.lock'
    lock_acquired = False

    # Prevent concurrent startup bumps from multiple processes.
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        lock_acquired = True
    except FileExistsError:
        print("[version] Startup bump skipped: another process is already updating VERSION.")
        return get_app_version()

    version = None
    try:
        if os.path.exists(vpath):
            with open(vpath, 'r', encoding='utf-8') as vf:
                version = (vf.read().strip() or None)
    except Exception:
        pass
    if not version:
        version = "00.00.01"
    old_version = version
    version = _bump_patch_version(version)
    try:
        with open(vpath, 'w', encoding='utf-8') as vf:
            vf.write(version)
        print(f"[version] Bumped: {old_version} -> {version}")
    except Exception as e:
        print(f"[version] Failed to write VERSION file: {e}")
    finally:
        if lock_acquired:
            try:
                os.remove(lock_path)
            except Exception:
                pass
    return version


def get_app_version(force_refresh=False):
    """Return app version exactly as stored in the VERSION file.

    force_refresh is accepted for backwards compatibility with callers.
    """
    default_version = "00.00.01"

    candidate_paths = []

    # Source/runtime next to app.py
    candidate_paths.append(os.path.join(os.path.dirname(__file__), 'VERSION'))

    # Current working directory (common in development and some launchers)
    candidate_paths.append(os.path.join(os.getcwd(), 'VERSION'))

    # Frozen executable directory (PyInstaller / installed app)
    if getattr(sys, 'frozen', False):
        candidate_paths.append(os.path.join(os.path.dirname(sys.executable), 'VERSION'))

    # De-duplicate while preserving order
    seen = set()
    ordered_paths = []
    for path in candidate_paths:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        ordered_paths.append(path)

    for version_file in ordered_paths:
        try:
            if os.path.exists(version_file):
                with open(version_file, 'r', encoding='utf-8') as vf:
                    raw = (vf.read().strip() or default_version)
                return raw
        except Exception:
            continue

    return default_version


# ================================================================
#  Register all route modules
# ================================================================
from routes.auth import register_auth_routes
from routes.license import register_license_routes
from routes.dashboard import register_dashboard_routes
from routes.students import register_student_routes
from routes.assistants import register_assistant_routes
from routes.schedule import register_schedule_routes
from routes.api import register_api_routes
from routes.qr import register_qr_routes
from routes.reports import register_reports_routes
from routes.books import register_book_routes
from routes.materials import register_material_routes
from routes.instructor_profile import register_instructor_profile_routes
from routes.setup import register_setup_routes

# Register scanner route
@app.route('/qr/scanner')
def qr_scanner():
    """Display QR code scanner input Name:Kennedy D.
    Name:Kennedy D.
    Name:Kennedy D.
    page for hardware barcode scanner."""
    return render_template('qr_scanner.html')


@app.route('/api/csrf-token')
def api_csrf_token():
    """Return a fresh CSRF token for AJAX retry flows."""
    return jsonify({'csrf_token': generate_csrf()})


@app.route('/healthz')
def healthz():
    """Health check endpoint for Render and uptime monitoring."""
    return jsonify({'status': 'ok'}), 200

# Register license routes first so activation is always reachable.
register_license_routes(app)

# Register legacy auth redirects/decorators for backwards compatibility.
register_auth_routes(app)

register_dashboard_routes(app)
register_student_routes(app, UPLOAD_FOLDER)
register_assistant_routes(app)
register_schedule_routes(app)
register_instructor_profile_routes(app)
register_api_routes(app)
register_qr_routes(app)
register_reports_routes(app)
register_book_routes(app)
register_material_routes(app)
register_setup_routes(app)




# ================================================================
#  Startup sanitation: ensure no lingering active sessions
# ================================================================
def _clear_state_on_startup():
    """On app start, stop any DB sessions left open and clear caches.
    This guarantees the Active Class column starts empty after a restart.
    """
    try:
        # Close any open DB sessions by setting end_time & duration (preserve history)
        try:
            closed = timer_manager.close_all_open_db_sessions()
            print(f'[startup] closed open DB sessions: {closed}')
        except Exception as e:
            print('[startup] close_all_open_db_sessions failed:', e)

    except Exception as e:
        print("[startup] clear_state error:", e)


_clear_state_on_startup()


# ================================================================
#  Auto-bump version on startup if source files changed
# ================================================================

def _check_version_on_startup():
    """Bump and log app version on every startup."""
    # In Flask debug reloader mode, skip parent process to avoid double-bumps.
    if (
        not IS_PRODUCTION
        and os.getenv("FLASK_USE_RELOADER", "true").lower() == "true"
        and os.getenv("WERKZEUG_RUN_MAIN") != "true"
    ):
        print("[version] Skipping startup bump in Flask reloader parent process.")
        return

    current_version = _ensure_version_up_to_date()
    recorded_version = record_app_version(current_version)
    if recorded_version != current_version:
        print(
            f"[version] Local app v{current_version} is older than recorded DB backup version v{recorded_version}."
        )
    # Push immediately so backup DB carries the latest compatible app version marker.
    sync_to_gdrive_now()
    print(f"[startup] Stdytime version: {current_version}")

_check_version_on_startup()


# ================================================================
#  Auto-generate QR codes for students and staff without them
# ================================================================
def _auto_generate_missing_qr_codes():
    """Generate QR codes for any students or staff that don't have them yet (stored in database)."""
    try:
        # Generate QR codes for students without them
        students = student_manager.get_all_students()
        student_count = 0
        for s in students:
            sid = s[0]
            name = s[1]
            # Check if QR code exists in database
            existing_qr = student_manager.get_student_qr_code(sid)
            if not existing_qr:
                try:
                    qr_data = f"ID:{sid}\nName:{name}"
                    qr_blob = qr_generator.generate_qr_bytes(qr_data)
                    student_manager.set_student_qr_code(sid, qr_blob)
                    student_count += 1
                except Exception as e:
                    print(f"[startup] Failed to generate QR for student {sid}: {e}")
        
        # Generate QR codes for staff without them
        assistants = assistant_manager.get_all_assistants()
        assistant_count = 0
        for a in assistants:
            aid = a[0]
            name = a[1]
            # Check if QR code exists in database
            existing_qr = assistant_manager.get_assistant_qr_code(aid)
            if not existing_qr:
                try:
                    qr_data = f"ASST:{aid}\nName:{name}"
                    qr_blob = qr_generator.generate_qr_bytes(qr_data)
                    assistant_manager.set_assistant_qr_code(aid, qr_blob)
                    assistant_count += 1
                except Exception as e:
                    print(f"[startup] Failed to generate QR for assistant {aid}: {e}")
        
        if student_count > 0 or assistant_count > 0:
            print(f'[startup] Auto-generated QR codes: {student_count} students, {assistant_count} staff')
    except Exception as e:
        print("[startup] auto_generate_qr_codes error:", e)


_auto_generate_missing_qr_codes()


# ================================================================
#  LemonSqueezy license verify on startup
# ================================================================
def _startup_verify_ls_license():
    try:
        from modules import ls_license as _ls
        valid, message = _ls.verify_ls_license(force=True)
        print(f"[startup] LS license: {message}")
    except Exception as exc:
        print(f"[startup] LS license check skipped: {exc}")

_startup_verify_ls_license()


# ================================================================
#  Exit/Shutdown Route
# ================================================================
@app.route("/exit", methods=["GET", "POST"])
def exit_app():
    """Handle application exit/shutdown with graceful browser window closure."""
    import threading
    import time

    global _EXIT_SHUTDOWN_IN_PROGRESS

    if os.getenv('ENABLE_PUBLIC_EXIT_ROUTE', 'false').lower() != 'true':
        return render_template("404.html"), 404

    # Prevent duplicate shutdown attempts from rapid repeated requests.
    if _EXIT_SHUTDOWN_IN_PROGRESS:
        return render_template("exit.html")
    _EXIT_SHUTDOWN_IN_PROGRESS = True
    
    print("\n[EXIT] User initiated application shutdown...")
    profiler.print_summary()

    werkzeug_shutdown = request.environ.get('werkzeug.server.shutdown')
    
    # Schedule shutdown after response is sent.
    # 1) Attempt graceful server stop when available (Werkzeug dev server).
    # 2) Force process exit as a reliability fallback (Waitress/other servers).
    def delayed_shutdown(shutdown_callable):
        time.sleep(0.8)

        # Hard-stop guard in case graceful shutdown hangs.
        def hard_stop_guard():
            time.sleep(4)
            print("[EXIT] Force-stopping application process...")
            os._exit(0)

        threading.Thread(target=hard_stop_guard, daemon=True, name="exit-hard-stop").start()

        if callable(shutdown_callable):
            try:
                print("[EXIT] Attempting graceful Werkzeug shutdown...")
                shutdown_callable()
                return
            except Exception as exc:
                print(f"[EXIT] Graceful shutdown failed: {exc}", file=sys.stderr)

        print("[EXIT] No graceful shutdown hook available; forcing process exit.")
        os._exit(0)
    
    shutdown_thread = threading.Thread(target=delayed_shutdown, args=(werkzeug_shutdown,), daemon=True, name="exit-shutdown")
    shutdown_thread.start()
    
    # Return exit page with JavaScript to close the window
    return render_template("exit.html")


# ================================================================
#  Error Handling
# ================================================================
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


# ================================================================
#  Shutdown handler - print profiler summary
# ================================================================

def print_profiler_summary():
    """Print request profiler summary on app shutdown."""
    profiler.print_summary()

atexit.register(print_profiler_summary)


# ================================================================
#  Run app
# ================================================================
if __name__ == "__main__":
    can_start_server = _acquire_single_instance_or_exit()
    if not can_start_server:
        # Exit naturally (code 0) to avoid debugger breaking on SystemExit.
        # For non-debug runs, return a non-zero process exit code.
        if _is_debugger_attached():
            pass
        else:
            raise SystemExit(1)
    else:

        port = int(os.getenv("PORT", "5000"))
        host = os.getenv("HOST", "127.0.0.1")
        if IS_PRODUCTION:
            # Production: use Waitress (no Flask dev-server warnings)
            from waitress import serve
            print(f"[Stdytime] Serving on http://{host}:{port}  (Waitress)")
            serve(app, host=host, port=port, threads=8)
        else:
            # Development: Flask dev server with reloader for fast iteration
            debugger_attached = _is_debugger_attached()
            if debugger_attached:
                print("[startup] Debugger detected; disabling Flask auto-reloader for a clean VS Code debug session.")
            use_reloader = (
                os.getenv("FLASK_USE_RELOADER", "true").lower() == "true"
                and not debugger_attached
            )
            app.run(
                host=host,
                port=port,
                debug=True,
                use_reloader=use_reloader,
                threaded=True,
            )
