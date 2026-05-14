#*****************************
#reports.py   ver 04--
#*****************************


from reportlab.pdfgen import canvas
import sqlite3, os
from modules.database import DB_PATH
from modules.utils import format_hhmm

def generate_report(title, entries, filename):
    os.makedirs("assets/reports", exist_ok=True)
    path = os.path.join("assets/reports", filename)
    c = canvas.Canvas(path)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(70, 800, title)
    c.setFont("Helvetica", 11)
    y = 770
    for e in entries:
        line = " | ".join(str(x) for x in e)
        c.drawString(70, y, line)
        y -= 18
        if y < 50:
            c.showPage(); c.setFont("Helvetica", 11); y = 800
    c.save();  return path

# --- Attendance summary for last 30 days ---
def get_student_attendance_summary(days=30):
    query = f"""
        SELECT s.name, s.subject,
               COUNT(sess.id) AS sessions,
               COALESCE(SUM(sess.duration),0)
        FROM students AS s
        LEFT JOIN sessions AS sess
          ON s.id = sess.student_id
         AND sess.start_time >= DATE('now','-{days} days')
                WHERE s.active = 1
                GROUP BY s.id
        ORDER BY s.name;
    """
    rows = []
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor(); c.execute(query)
        for (name, subj, count, total_sec) in c.fetchall():
            rows.append((name, subj, count, format_hhmm(total_sec)))
    return rows


# --- Staff hours summary for last N days ---
def get_assistant_hours_summary(days=30):
    days = int(days)
    query = f"""
        SELECT st.name,
               COUNT(a.id) AS sessions,
               COALESCE(SUM(a.duration),0) AS total_seconds
        FROM staff AS st
        LEFT JOIN assistant_sessions AS a
          ON st.id = a.assistant_id
         AND a.start_time >= DATE('now','-{days} days')
        GROUP BY st.id
        ORDER BY st.name;
    """
    rows = []
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor(); c.execute(query)
        for (name, count, total_sec) in c.fetchall():
            rows.append((name, count, format_hhmm(total_sec)))
    return rows


def generate_assistant_hours_report(days=30, filename=None):
    rows = []
    data = get_assistant_hours_summary(days)
    for r in data:
        rows.append(r)
    if not filename:
        filename = f"assistant_hours_{days}d.pdf"
    title = f"Staff Hours Summary (last {days} days)"
    return generate_report(title, rows, filename)


def get_assistant_hours_between(start_date, end_date):
    """Return (name, sessions, total_seconds) for assistants between two dates (inclusive).
    Dates should be YYYY-MM-DD strings.
    """
    query = """
        SELECT st.name,
               COUNT(a.id) AS sessions,
               COALESCE(SUM(a.duration),0) AS total_seconds
        FROM staff AS st
        LEFT JOIN assistant_sessions AS a
          ON st.id = a.assistant_id
         AND DATE(a.start_time) BETWEEN ? AND ?
        GROUP BY st.id
        ORDER BY st.name;
    """
    rows = []
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor(); c.execute(query, (start_date, end_date))
        for (name, sessions, total_sec) in c.fetchall():
            rows.append((name, sessions, total_sec))
    return rows


def get_assistant_sessions_between(start_date, end_date):
    """Return detailed assistant sessions between two dates (inclusive).
    Each row is (name, date, start_iso, end_iso, duration_seconds).
    Dates should be YYYY-MM-DD strings.
    """
    query = """
        SELECT st.name,
               a.start_time,
               a.end_time,
               a.duration
        FROM assistant_sessions AS a
        JOIN staff AS st ON st.id = a.assistant_id
        WHERE DATE(a.start_time) BETWEEN ? AND ?
        ORDER BY st.name, a.start_time;
    """
    rows = []
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor(); c.execute(query, (start_date, end_date))
        for (name, start_iso, end_iso, duration) in c.fetchall():
            date_only = None
            try:
                date_only = start_iso.split('T')[0]
            except Exception:
                date_only = start_iso
            rows.append((name, date_only, start_iso, end_iso, int(duration or 0)))
    return rows