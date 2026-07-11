#!/usr/bin/env python3
"""
Stdytime Local Launcher
Starts the app and opens browser to http://127.0.0.1:5000/
"""

import os
import sys
import time
import webbrowser
import subprocess
import shutil
import tempfile
import socket
import ctypes
from pathlib import Path
from dotenv import load_dotenv


_DEFAULT_HOST = '0.0.0.0'
_DEFAULT_PORT = 5000


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
    """Launch a dedicated browser process when possible."""
    browser_exe = _find_browser_executable()
    if not browser_exe:
        webbrowser.open(url)
        return None

    browser_name = Path(browser_exe).stem.lower()
    profile_dir = os.path.join(tempfile.gettempdir(), "stdytime_browser_profile")
    os.makedirs(profile_dir, exist_ok=True)

    if "firefox" in browser_name:
        cmd = [browser_exe, "-new-instance", "-profile", profile_dir, "-new-window", url]
    else:
        # Chrome/Edge: keep a dedicated process we can wait on.
        cmd = [browser_exe, f"--user-data-dir={profile_dir}", "--new-window", url]

    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)
        return None


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
            f"Instance already running on {_DEFAULT_HOST}:{_DEFAULT_PORT}.\n\nOpening existing instance:\n{url}",
            "Stdytime",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass

def main():
    _load_local_env()

    # Local-safe defaults only when missing.
    os.environ.setdefault('APP_ENV', 'development')
    os.environ.setdefault('HOST', _DEFAULT_HOST)
    os.environ.setdefault('PORT', str(_DEFAULT_PORT))
    os.environ.setdefault('COOKIE_SECURE', 'false')

    host = os.environ.get('HOST', _DEFAULT_HOST)
    port = int(os.environ.get('PORT', str(_DEFAULT_PORT)))
    browser_host = _browser_host_for(host)
    url = f"http://{browser_host}:{port}/"
    probe_host = '127.0.0.1' if host in {'0.0.0.0', '::'} else host

    # If the app is already running, do not spawn another instance.
    if _is_port_open(probe_host, port):
        print(f"\nInstance already running on {host}:{port}")
        print("Opening the existing instance in your browser...")
        _show_already_running_notice(url)
        _launch_browser(url)
        return

    # Get the app directory
    if getattr(sys, 'frozen', False):
        # Running as executable
        app_dir = sys.executable
        if app_dir.endswith('.exe'):
            app_dir = str(Path(app_dir).parent)
    else:
        # Running as script
        app_dir = str(Path(__file__).parent)
    
    print("\n" + "="*50)
    print("Stdytime - Local Server")
    print("="*50)
    print("\nStarting app...")
    
    # Launch the app
    try:
        # Run Stdytime.exe or app.py depending on context
        exe_path = os.path.join(app_dir, 'Stdytime.exe')
        if os.path.exists(exe_path):
            # Running packaged version - start the subprocess in a way that
            # doesn't block this script
            import threading
            thread = threading.Thread(
                target=subprocess.run,
                args=([exe_path],),
                kwargs={'capture_output': False},
                daemon=True
            )
            thread.start()
        else:
            # Fall back to running app.py directly
            import threading
            thread = threading.Thread(
                target=subprocess.run,
                args=([sys.executable, os.path.join(app_dir, 'app.py')],),
                kwargs={'capture_output': False},
                daemon=True
            )
            thread.start()
        
        # Wait for app to start
        print("Waiting for server to start...")
        time.sleep(2)
        
        # Open browser

        # Try to read center name from config or database
        center_name = None
        db_config_path = os.path.join(app_dir, 'db_config.json')
        if os.path.exists(db_config_path):
            import json
            try:
                with open(db_config_path, encoding='utf-8') as f:
                    cfg = json.load(f)
                    center_name = cfg.get('center_name')
            except Exception:
                pass
        if not center_name:
            # Try to read from instructor_profile if available
            try:
                import sqlite3
                db_path = os.path.join(app_dir, 'data', 'Stdytime.db')
                if os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT center_location FROM instructor_profile LIMIT 1")
                    row = cur.fetchone()
                    if row and row[0]:
                        center_name = row[0]
                    conn.close()
            except Exception:
                pass
        if not center_name:
            center_name = "Stdytime Center"

        print(f"\nOpening {url} in your browser...")
        browser_proc = _launch_browser(url)

        print("\n" + "="*50)
        print(f"Welcome to: {center_name}")
        print("="*50)
        print(f"\nAccess at: {url}")
        if browser_proc:
            print("\nClose the browser window to stop the app and free port 5000.")
        else:
            print("\nClose this window to stop the app and free port 5000.")
        print("\n" + "="*50 + "\n")
        
        # Keep the script running until the dedicated browser process exits.
        # If we couldn't get a process handle (fallback browser), stay alive until Ctrl+C.
        try:
            if browser_proc:
                browser_proc.wait()
            else:
                while True:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            sys.exit(0)
            
    except Exception as e:
        print(f"\nError starting app: {e}", file=sys.stderr)
        print("\nMake sure you have extracted the Stdytime package properly.", file=sys.stderr)
        input("\nPress Enter to close...")
        sys.exit(1)

if __name__ == '__main__':
    main()
