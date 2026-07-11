#*****************************
#staff_manager.py   ver 04--
#*****************************


import sqlite3
from modules.database import DB_PATH

def get_all_staff():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT id,name,role FROM staff ORDER BY name;")
        return c.fetchall()