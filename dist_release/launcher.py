"""
Stdytime launcher — starts a Waitress WSGI server and shows a system tray icon.

Usage:
    python launcher.py          # starts on default port (5000)
    python launcher.py 8080     # starts on a custom port

The tray icon provides:
    Open Stdytime   — opens the browser
    ─────────────
    Quit            — stops the server and exits
"""

import sys
import os
import threading
import webbrowser
import logging
import subprocess
import tempfile
import shutil
import socket
import ctypes
import importlib.util
from pathlib import Path
from dotenv import load_dotenv


def _ensure_app_import_path() -> None:
    """Ensure the app directory is importable in script and frozen runs."""
    candidate_dirs = []

    try:
        candidate_dirs.append(Path(__file__).resolve().parent)
    except Exception:
        pass

    try:
        candidate_dirs.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass

    # PyInstaller onefile extraction directory.
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        try:
            candidate_dirs.append(Path(meipass).resolve())
        except Exception:
            pass

    try:
        candidate_dirs.append(Path.cwd().resolve())
    except Exception:
        pass

    seen = set()
    ordered = []
    for directory in candidate_dirs:
        if not directory:
            continue
        key = os.path.normcase(str(directory))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(str(directory))

    for directory in reversed(ordered):
        if directory not in sys.path:
            sys.path.insert(0, directory)


_ensure_app_import_path()


def _load_local_env() -> None:
    """Load .env from app directory when available."""
    env_path = Path(__file__).with_name('.env')
    try:
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)
    except Exception:
        pass


def _detect_lan_ip() -> str:
    """Best-effort local LAN IPv4 address for same-WiFi access."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return '127.0.0.1'


def _browser_host_for(listen_host: str) -> str:
    """Translate bind host into a browser-friendly host."""
    host = str(listen_host or '').strip().lower()
    if host in {'0.0.0.0', '::', ''}:
        preferred = str(os.getenv('LAUNCH_HOST', '') or '').strip()
        if preferred:
            return preferred
        return _detect_lan_ip()
    return listen_host


_load_local_env()

# ---------------------------------------------------------------------------
# Port/Host — CLI arg or env var, default 5000/0.0.0.0
# ---------------------------------------------------------------------------
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("PORT", "5000"))
HOST = os.getenv("HOST", "0.0.0.0")
URL  = f"http://{_browser_host_for(HOST)}:{PORT}"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("launcher")

# ---------------------------------------------------------------------------
# Waitress server thread
# ---------------------------------------------------------------------------
_server = None
_server_thread = None
_tray_icon = None
_browser_process = None
_browser_monitor_started = False
_is_quitting = False


def _should_shutdown_on_browser_exit() -> bool:
    """Whether backend should auto-stop when tracked browser process exits.

    Default is disabled because some browsers spawn short-lived launcher
    processes during startup, which can cause false shutdowns.
    """
    return os.getenv("STDYTIME_SHUTDOWN_ON_BROWSER_EXIT", "false").strip().lower() == "true"


def _build_icon_image():
    """Generate a simple coloured square as the tray icon (no external file needed)."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Green rounded square
    draw.rounded_rectangle([4, 4, size - 4, size - 4], radius=12, fill="#28a745")
    # White letter S
    draw.text((20, 14), "S", fill="white")
    return img


def _load_icon_image():
    """Use app logo if it exists, otherwise generate one."""
    logo_path = os.path.join(os.path.dirname(__file__), "static", "img", "logo.png")
    if os.path.isfile(logo_path):
        from PIL import Image
        return Image.open(logo_path).convert("RGBA").resize((64, 64))
    return _build_icon_image()


def start_server():
    global _server
    from waitress import create_server

    def _load_wsgi_app():
        try:
            from app import app as flask_app
            return flask_app
        except ModuleNotFoundError as exc:
            # Only handle missing top-level app module here.
            if getattr(exc, 'name', None) != 'app':
                raise

        candidate_app_files = []
        for raw_base in (
            getattr(sys, '_MEIPASS', None),
            Path(__file__).resolve().parent,
            Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else None,
            Path.cwd(),
        ):
            if not raw_base:
                continue
            try:
                base = Path(raw_base).resolve()
            except Exception:
                continue
            app_file = base / 'app.py'
            if app_file.exists():
                candidate_app_files.append(app_file)

        seen_files = set()
        for app_file in candidate_app_files:
            key = os.path.normcase(str(app_file))
            if key in seen_files:
                continue
            seen_files.add(key)
            try:
                spec = importlib.util.spec_from_file_location('stdytime_runtime_app', str(app_file))
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    flask_app = getattr(module, 'app', None)
                    if flask_app is not None:
                        log.info("Loaded app via fallback file: %s", app_file)
                        return flask_app
            except Exception as fallback_exc:
                log.warning("Fallback app load failed from %s: %s", app_file, fallback_exc)

        log.exception(
            "Could not import 'app'. launcher_dir=%s; executable_dir=%s; meipass=%s; cwd=%s; sys.path[0]=%s",
            Path(__file__).resolve().parent,
            Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else '',
            getattr(sys, '_MEIPASS', ''),
            Path.cwd(),
            sys.path[0] if sys.path else '',
        )
        raise ModuleNotFoundError("No module named 'app'")

    app = _load_wsgi_app()

    log.info("Starting Waitress on %s", URL)
    _server = create_server(app, host=HOST, port=PORT, threads=8)
    _server.run()


def _is_port_open(host, port, timeout=0.8):
    """Return True when a server is already listening on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _show_already_running_notice(url):
    """Show a user-facing notice in packaged mode when an instance is already running."""
    if not getattr(sys, 'frozen', False):
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Instance already running on {HOST}:{PORT}.\n\nOpening existing instance:\n{url}",
            "Stdytime",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


def _find_browser_executable():
    """Prefer a browser executable we can monitor as a process."""
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("firefox"),
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _launch_browser(url):
    """Launch a dedicated browser process when possible so it can be monitored."""
    browser_exe = _find_browser_executable()
    if not browser_exe:
        webbrowser.open(url)
        return None

    browser_name = Path(browser_exe).stem.lower()
    profile_dir = os.path.join(tempfile.gettempdir(), "stdytime_tray_browser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    if "firefox" in browser_name:
        cmd = [browser_exe, "-new-instance", "-profile", profile_dir, "-new-window", url]
    else:
        # Chrome/Edge: isolate profile + disable background mode so process exits on window close.
        cmd = [
            browser_exe,
            f"--user-data-dir={profile_dir}",
            "--new-window",
            "--disable-background-mode",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]

    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        log.warning("Could not launch dedicated browser process (%s). Falling back.", exc)
        webbrowser.open(url)
        return None


def _monitor_browser_exit():
    """Shut down the app when the dedicated browser process exits."""
    global _browser_process
    proc = _browser_process
    if not proc:
        return

    try:
        proc.wait()
        if not _is_quitting and _should_shutdown_on_browser_exit():
            log.info("Browser closed. Stopping Stdytime backend.")
            quit_app(_tray_icon)
        elif not _is_quitting:
            log.info("Browser process exited; backend remains running.")
    except Exception as exc:
        log.debug("Browser monitor ended with warning: %s", exc)


def open_browser():
    global _browser_process, _browser_monitor_started

    # If a dedicated browser instance is still alive, do not spawn a duplicate.
    if _browser_process and _browser_process.poll() is None:
        return

    _browser_process = _launch_browser(URL)
    if _browser_process and not _browser_monitor_started and _should_shutdown_on_browser_exit():
        _browser_monitor_started = True
        threading.Thread(target=_monitor_browser_exit, daemon=True, name="browser-monitor").start()


def quit_app(icon, item=None):
    global _is_quitting
    if _is_quitting:
        return
    _is_quitting = True

    log.info("Shutting down...")
    if icon:
        icon.stop()
    if _server:
        _server.close()
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global _server_thread, _tray_icon

    if _is_port_open(HOST, PORT):
        log.info("Instance already running on %s:%s; opening existing browser session.", HOST, PORT)
        _show_already_running_notice(URL)
        open_browser()
        return

    # Start Waitress in a daemon thread
    _server_thread = threading.Thread(target=start_server, daemon=True, name="waitress")
    _server_thread.start()
    log.info("Stdytime running at %s", URL)

    # Open browser after a longer delay on startup to reduce first-run races.
    threading.Timer(4.0, open_browser).start()

    # System tray icon
    try:
        import pystray
        from pystray import MenuItem as Item, Menu

        icon_image = _load_icon_image()

        menu = Menu(
            Item("Open Stdytime", lambda icon, item: open_browser(), default=True),
            Menu.SEPARATOR,
            Item("Quit", quit_app),
        )

        _tray_icon = pystray.Icon("Stdytime", icon_image, "Stdytime", menu)
        _tray_icon.run()          # blocks until quit_app() calls icon.stop()

    except Exception as exc:
        log.warning("Tray icon unavailable (%s). Running headless — press Ctrl+C to quit.", exc)
        # No tray: keep the main thread alive
        try:
            _server_thread.join()
        except KeyboardInterrupt:
            quit_app(None)


if __name__ == "__main__":
    main()
