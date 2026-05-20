# routes/dashboard.py
from flask import render_template
from modules import student_manager, auth_manager
from routes.auth import require_login, require_feature
import sqlite3
from modules.database import DB_PATH
from datetime import datetime

def register_dashboard_routes(app):
    """Register dashboard and helper routes."""
    
    def get_active_students():
        """Return (name, subject, level) for currently active students."""
        rows = student_manager.get_all_students()
        active_list = []
        for r in rows:
            sid = r[0]
            active_flag = None
            if len(r) >= 8:
                active_flag = r[7]
            else:
                with sqlite3.connect(DB_PATH) as conn:
                    active_row = conn.execute(
                        "SELECT active FROM students WHERE id=?",
                        (sid),
                    ).fetchone()
                    active_flag = active_row[0] if active_row else 0
            if active_flag == 1:
                name, subj, lvl = r[1], r[2], r[3]
                active_list.append((name, subj, lvl))
        return active_list

    @app.route("/")
    @require_login
    @require_feature(auth_manager.FEATURE_STDYTIMECLASS)
    def dashboard():
        return render_template(
            "dashboard.html",
            active_students=get_active_students(),
            assistants=[
                ("Sarah Chen", "2 h 15 m"),
                ("Mike Johnson", "1 h 42 m"),
                ("Emma Davis", "3 h 08 m"),
            ],
            checked_out=[
                ("Bob Smith", "2 h 45 m"),
                ("Julia White", "1 h 30 m"),
                ("Kevin Taylor", "3 h 15 m"),
            ],
        )

    @app.context_processor
    def inject_now():
        """Inject current date/time into all templates."""
        now = datetime.now()
        return dict(
            date_str=now.strftime("%A, %B %d, %Y"),
            time_str=now.strftime("%I:%M:%S %p"),
        )
