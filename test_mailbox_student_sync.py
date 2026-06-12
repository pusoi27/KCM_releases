#!/usr/bin/env python3
"""
Regression test for mailbox-based student propagation:
Admin_Outbox must carry student snapshots so scanner station can see
students created on admin/instructor station.
"""

import json
import os
import sqlite3
import tempfile
import shutil

from modules import database


def _create_test_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                role TEXT,
                email TEXT,
                phone TEXT,
                loading INTEGER DEFAULT 1
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                student_identifier TEXT DEFAULT '',
                subject TEXT,
                subjects_json TEXT DEFAULT '[]',
                subject_minutes_json TEXT DEFAULT '[]',
                total_study_minutes INTEGER DEFAULT 30,
                book_loaned INTEGER DEFAULT 0,
                email TEXT,
                phone TEXT,
                guardian TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                el INTEGER DEFAULT 0,
                pi INTEGER DEFAULT 0,
                v INTEGER DEFAULT 0,
                day1 TEXT DEFAULT '',
                day1_time TEXT DEFAULT '',
                day2 TEXT DEFAULT '',
                day2_time TEXT DEFAULT '',
                day3 TEXT DEFAULT '',
                day3_time TEXT DEFAULT '',
                day4 TEXT DEFAULT '',
                day4_time TEXT DEFAULT '',
                day5 TEXT DEFAULT '',
                day5_time TEXT DEFAULT '',
                day6 TEXT DEFAULT '',
                day6_time TEXT DEFAULT '',
                checkout_notify_enabled INTEGER DEFAULT 1,
                photo_blob BLOB,
                photo_mime TEXT DEFAULT '',
                schedule_json TEXT DEFAULT '',
                qr_code BLOB,
                device_loaned INTEGER DEFAULT 0,
                ind INTEGER DEFAULT 0
            )
            """
        )
        conn.commit()


def _seed_admin_data(admin_db: str) -> None:
    with sqlite3.connect(admin_db) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO staff (name, role, email, phone, loading)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Admin User", "Admin", "admin@example.com", "555-1000", 1),
        )
        cur.execute(
            """
            INSERT INTO students (
                name, student_identifier, subject, subjects_json, subject_minutes_json,
                total_study_minutes, email, phone, guardian, active,
                el, pi, v, ind,
                day1, day1_time, day2, day2_time, day3, day3_time,
                day4, day4_time, day5, day5_time, day6, day6_time,
                schedule_json, checkout_notify_enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Test Student",
                "STU1001",
                "Math",
                json.dumps(["Math", "Reading"]),
                json.dumps([30, 30]),
                60,
                "student@example.com",
                "555-2000",
                "Guardian Name",
                1,
                0,
                1,
                0,
                0,
                "Monday",
                "15:00",
                "Wednesday",
                "16:00",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                json.dumps([
                    {"day": "Monday", "time": "15:00"},
                    {"day": "Wednesday", "time": "16:00"},
                ]),
                1,
            ),
        )
        conn.commit()


def test_mailbox_admin_snapshot_includes_students_and_scanner_imports_them() -> None:
    tmp = tempfile.mkdtemp(prefix="stdytime_mailbox_test_")
    try:
        admin_db = os.path.join(tmp, "admin.db")
        scanner_db = os.path.join(tmp, "scanner.db")
        admin_outbox = os.path.join(tmp, "Admin_Outbox")
        archive_admin = os.path.join(tmp, "Archive", "Admin_Outbox")
        os.makedirs(admin_outbox, exist_ok=True)
        os.makedirs(archive_admin, exist_ok=True)

        _create_test_db(admin_db)
        _create_test_db(scanner_db)
        _seed_admin_data(admin_db)

        exported = database._admin_export_staff_snapshot(admin_db, admin_outbox)
        assert exported is True, "Expected reference snapshot export to occur"

        payload_files = [
            os.path.join(admin_outbox, name)
            for name in sorted(os.listdir(admin_outbox))
            if name.lower().endswith(".json")
        ]
        assert payload_files, "Expected at least one Admin_Outbox payload"

        with open(payload_files[0], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload.get("kind") == "admin_reference", "Expected admin_reference payload kind"
        assert isinstance(payload.get("students"), list), "Payload must include students list"
        assert any((row.get("student_identifier") or "") == "STU1001" for row in payload["students"])

        imported_count = database._scanner_import_admin_outbox(scanner_db, admin_outbox, archive_admin)
        assert imported_count == 1, f"Expected one payload imported, got {imported_count}"

        with sqlite3.connect(scanner_db) as conn:
            row = conn.execute(
                """
                SELECT name, student_identifier, email, phone, active
                FROM students
                WHERE student_identifier = ?
                LIMIT 1
                """,
                ("STU1001",),
            ).fetchone()

        assert row is not None, "Scanner DB should contain synced student"
        assert row[0] == "Test Student"
        assert row[1] == "STU1001"
        assert row[2] == "student@example.com"
        assert row[3] == "555-2000"
        assert int(row[4] or 0) == 1
    finally:
        # Windows can keep brief transient locks after sqlite operations;
        # cleanup is best-effort and should not fail the regression signal.
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    test_mailbox_admin_snapshot_includes_students_and_scanner_imports_them()
    print("✓ Mailbox student sync regression test passed")
