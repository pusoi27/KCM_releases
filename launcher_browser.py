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
from pathlib import Path

def main():
    # Set environment for local development
    os.environ['APP_ENV'] = 'development'
    os.environ['HOST'] = '127.0.0.1'
    os.environ['PORT'] = '5000'
    os.environ['COOKIE_SECURE'] = 'false'
    
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

        url = "http://127.0.0.1:5000/"
        print(f"\nOpening {url} in your browser...")
        webbrowser.open(url)

        print("\n" + "="*50)
        print(f"Welcome to: {center_name}")
        print("="*50)
        print(f"\nAccess at: {url}")
        print("\nClose this window to stop the app.")
        print("\n" + "="*50 + "\n")
        
        # Keep the script running
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down...")
            sys.exit(0)
            
    except Exception as e:
        print(f"\nError starting app: {e}", file=sys.stderr)
        print("\nMake sure you have extracted the Stdytime package properly.", file=sys.stderr)
        input("\nPress Enter to close...")
        sys.exit(1)

if __name__ == '__main__':
    main()
