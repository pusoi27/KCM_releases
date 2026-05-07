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

# Load environment variables from .env file
load_dotenv()

from modules.database import init_db, DB_PATH
from modules import student_manager, timer_manager, qr_generator, assistant_manager, reports, auth_manager, license_manager
from modules import instructor_profile_manager
from modules import user_identity_manager
from modules import server_cache
from modules.utils import format_hhmm
from modules.rate_limiter import limiter

# ================================================================
#  Flask setup
# ================================================================
app = Flask(__name__)

IS_PRODUCTION = (
    os.getenv('APP_ENV', 'development').lower() == 'production'
    or os.getenv('RENDER', '').lower() == 'true'
)

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
            f"📖 Total READs  (GET):                 {self.total_reads}\n"
            f"✏️  Total WRITEs (POST/PUT/DELETE):   {self.total_writes}\n"
            f"📊 Total Requests:                     {total}\n"
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

@app.after_request
def after_request_profiler(response):
    """Log request after it completes."""
    from flask import g
    endpoint = request.path
    profiler.log_request(request.method, endpoint, response.status_code)
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
        'can_access_kumoclass': True,
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
    return dict(app_version=_ensure_version_up_to_date())


@app.context_processor
def inject_branding():
    """Inject profile-based branding and shared theme values into templates."""
    profile = None
    try:
        profile = instructor_profile_manager.get_instructor_profile()
    except Exception:
        profile = None

    center_name = (profile.get('center_location') if profile else None) or 'Stdytime'
    return dict(
        branding_profile=profile,
        branding_center_name=center_name,
        brand_primary='#2e7d32',
        brand_primary_dark='#1b5e20',
        brand_accent='#fdd835',
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
    """Read version from VERSION file, check if source files changed, and auto-bump if needed."""
    vpath = os.path.join(os.path.dirname(__file__), 'VERSION')
    
    # Try to read from VERSION file first
    version = None
    try:
        if os.path.exists(vpath):
            with open(vpath, 'r', encoding='utf-8') as vf:
                version = (vf.read().strip() or None)
    except Exception:
        pass
    
    # Fallback: if no VERSION file or empty, create default
    if not version:
        version = "00.00.01"
        try:
            with open(vpath, 'w', encoding='utf-8') as vf:
                vf.write(version)
        except Exception:
            pass
    
    try:
        latest_mtime = _find_latest_source_mtime(os.path.dirname(__file__))
        v_mtime = os.path.getmtime(vpath) if os.path.exists(vpath) else 0
        if latest_mtime > v_mtime:
            old_version = version
            version = _bump_patch_version(version)
            try:
                with open(vpath, 'w', encoding='utf-8') as vf:
                    vf.write(version)
                print(f"[version] Bumped: {old_version} → {version}")
            except Exception as e:
                print(f"[version] Failed to write VERSION file: {e}")
    except Exception as e:
        print(f"[version] Error checking version: {e}")
    return version


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
from routes.instructor_profile import register_instructor_profile_routes

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
    """Check and auto-bump version on app startup if source files have changed."""
    current_version = _ensure_version_up_to_date()
    print(f"[startup] Stdytime version: {current_version}")

if os.getenv('ENABLE_VERSION_AUTOBUMP', 'true').lower() == 'true':
    _check_version_on_startup()


# ================================================================
#  Auto-generate QR codes for students and staff without them
# ================================================================
def _auto_generate_missing_qr_codes():
    """Generate QR codes for any students or staff that don't have them yet."""
    try:
        import os
        out_dir = os.path.join('assets', 'qr_codes')
        os.makedirs(out_dir, exist_ok=True)
        
        # Generate QR codes for students without them
        students = student_manager.get_all_students()
        student_count = 0
        for s in students:
            sid = s[0]
            name = s[1]
            qr_path = os.path.join(out_dir, f"student_{sid}.png")
            if not os.path.exists(qr_path):
                try:
                    qr_data = f"ID:{sid}\nName:{name}"
                    qr_generator.generate_qr(qr_data, f"student_{sid}")
                    student_count += 1
                except Exception as e:
                    print(f"[startup] Failed to generate QR for student {sid}: {e}")
        
        # Generate QR codes for staff without them
        assistants = assistant_manager.get_all_assistants()
        assistant_count = 0
        for a in assistants:
            aid = a[0]
            name = a[1]
            qr_path = os.path.join(out_dir, f"assistant_{aid}.png")
            if not os.path.exists(qr_path):
                try:
                    qr_data = f"ASST:{aid}\nName:{name}"
                    qr_generator.generate_qr(qr_data, f"assistant_{aid}")
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
    import sys
    import threading

    if os.getenv('ENABLE_PUBLIC_EXIT_ROUTE', 'false').lower() != 'true':
        return render_template("404.html"), 404
    
    print("\n[EXIT] User initiated application shutdown...")
    profiler.print_summary()
    
    # Schedule shutdown after response is sent (1 second delay)
    def delayed_shutdown():
        import time
        time.sleep(1)
        print("[EXIT] Closing application...")
        sys.exit(0)
    
    shutdown_thread = threading.Thread(target=delayed_shutdown, daemon=True)
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
import atexit

def print_profiler_summary():
    """Print request profiler summary on app shutdown."""
    profiler.print_summary()

atexit.register(print_profiler_summary)


# ================================================================
#  Run app
# ================================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    if IS_PRODUCTION:
        # Production: use Waitress (no Flask dev-server warnings)
        from waitress import serve
        print(f"[Stdytime] Serving on http://127.0.0.1:{port}  (Waitress)")
        serve(app, host="127.0.0.1", port=port, threads=8)
    else:
        # Development: Flask dev server with reloader for fast iteration
        app.run(
            host="127.0.0.1",
            port=port,
            debug=True,
            use_reloader=True,
            threaded=True,
        )
