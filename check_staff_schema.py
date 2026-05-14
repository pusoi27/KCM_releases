#!/usr/bin/env python3
"""Check staff table schema."""
import sqlite3

conn = sqlite3.connect('C:/Users/octav/AppData/Local/StdyTime/Stdytime.db')
c = conn.cursor()
c.execute("PRAGMA table_info(staff);")
print("STAFF TABLE SCHEMA:")
print("-" * 50)
for row in c.fetchall():
    col_id, col_name, col_type, col_notnull, col_default, col_pk = row
    print(f"{col_name:20} {col_type:10}")
print("-" * 50)
conn.close()
