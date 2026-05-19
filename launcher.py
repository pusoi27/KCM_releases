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
from pathlib import Path

# ---------------------------------------------------------------------------
# Port/Host — CLI arg or env var, default 5000/127.0.0.1
# ---------------------------------------------------------------------------
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("PORT", "5000"))
HOST = os.getenv("HOST", "127.0.0.1")
URL  = f"http://{HOST}:{PORT}"

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
# Import Flask app (this runs module-level setup in app.py)
# ---------------------------------------------------------------------------
from app import app  # noqa: E402

# ---------------------------------------------------------------------------
# Waitress server thread
# ---------------------------------------------------------------------------
_server = None
_server_thread = None
_tray_icon = None
_browser_process = None
_browser_monitor_started = False
_is_quitting = False


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
    log.info("Starting Waitress on %s", URL)
    _server = create_server(app, host=HOST, port=PORT, threads=8)
    _server.run()


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
        if not _is_quitting:
            log.info("Browser closed. Stopping Stdytime backend.")
            quit_app(_tray_icon)
    except Exception as exc:
        log.debug("Browser monitor ended with warning: %s", exc)


def open_browser():
    global _browser_process, _browser_monitor_started

    # If a dedicated browser instance is still alive, do not spawn a duplicate.
    if _browser_process and _browser_process.poll() is None:
        return

    _browser_process = _launch_browser(URL)
    if _browser_process and not _browser_monitor_started:
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
    os._exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global _server_thread, _tray_icon

    # Start Waitress in a daemon thread
    _server_thread = threading.Thread(target=start_server, daemon=True, name="waitress")
    _server_thread.start()
    log.info("Stdytime running at %s", URL)

    # Open browser after a short delay (let server bind first)
    threading.Timer(1.2, open_browser).start()

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
