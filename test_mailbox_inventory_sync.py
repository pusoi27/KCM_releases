#!/usr/bin/env python3
"""
Regression test for mailbox-based inventory propagation:
Admin_Outbox must carry books/devices with borrower (loan state), and scanner
imports must be idempotent.
"""

import json
import os
import shutil
import sqlite3
import tempfile

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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                author TEXT,
                isbn TEXT,
                isbn13 TEXT,
                publisher TEXT,
                available INTEGER DEFAULT 1,
                reading_level TEXT,
                copies INTEGER DEFAULT 1,
                borrower_id INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                author TEXT,
                qr_code TEXT,
                publisher TEXT,
                available INTEGER DEFAULT 1,
                reading_level TEXT,
                copies INTEGER DEFAULT 1,
                borrower_id INTEGER
            )
            """
        )
        conn.commit()


def _seed_admin_inventory(admin_db: str) -> None:
    with sqlite3.connect(admin_db) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO students (
                name, student_identifier, subject, subjects_json, subject_minutes_json,
                total_study_minutes, email, phone, guardian, active,
                el, pi, v, ind, book_loaned, device_loaned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Inventory Student",
                "INV1001",
                "Math",
                json.dumps(["Math"]),
                json.dumps([30]),
                30,
                "invstudent@example.com",
                "555-3010",
                "Guardian I",
                1,
                0,
                1,
                0,
                0,
                1,
                1,
            ),
        )
        student_id = cur.lastrowid

        cur.execute(
            """
            INSERT INTO books (
                title, author, isbn, isbn13, publisher,
                available, reading_level, copies, borrower_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Core Math Workbook",
                "Kumo",
                "1112223334",
                "9781112223330",
                "KP",
                0,
                "5A",
                1,
                student_id,
            ),
        )

        cur.execute(
            """
            INSERT INTO materials (
                title, author, qr_code, publisher,
                available, reading_level, copies, borrower_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "iPad A",
                "Apple",
                "MAT-000001-ABC123",
                "Apple",
                0,
                "N/A",
                1,
                student_id,
            ),
        )
        conn.commit()


def _write_payload(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def test_mailbox_inventory_sync_and_idempotency() -> None:
    tmp = tempfile.mkdtemp(prefix="stdytime_mailbox_inventory_test_")
    try:
        admin_db = os.path.join(tmp, "admin.db")
        scanner_db = os.path.join(tmp, "scanner.db")
        admin_outbox = os.path.join(tmp, "Admin_Outbox")
        archive_admin = os.path.join(tmp, "Archive", "Admin_Outbox")
        os.makedirs(admin_outbox, exist_ok=True)
        os.makedirs(archive_admin, exist_ok=True)

        _create_test_db(admin_db)
        _create_test_db(scanner_db)
        _seed_admin_inventory(admin_db)

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

        assert payload.get("kind") == "admin_reference"
        assert isinstance(payload.get("books"), list), "Payload must include books list"
        assert isinstance(payload.get("materials"), list), "Payload must include materials list"
        assert any((row.get("isbn") or "") == "1112223334" for row in payload["books"])
        assert any((row.get("qr_code") or "") == "MAT-000001-ABC123" for row in payload["materials"])

        imported_count = database._scanner_import_admin_outbox(scanner_db, admin_outbox, archive_admin)
        assert imported_count == 1, f"Expected one payload imported, got {imported_count}"

        with sqlite3.connect(scanner_db) as conn:
            conn.row_factory = sqlite3.Row
            student = conn.execute(
                "SELECT id, student_identifier, book_loaned, device_loaned FROM students WHERE student_identifier = ? LIMIT 1",
                ("INV1001",),
            ).fetchone()
            assert student is not None, "Scanner DB should contain synced student"

            book = conn.execute(
                "SELECT title, isbn, borrower_id, available FROM books WHERE isbn = ? LIMIT 1",
                ("1112223334",),
            ).fetchone()
            assert book is not None, "Scanner DB should contain synced book"
            assert int(book[2] or 0) == int(student["id"])
            assert int(book[3] or 0) == 0

            material = conn.execute(
                "SELECT title, qr_code, borrower_id, available FROM materials WHERE qr_code = ? LIMIT 1",
                ("MAT-000001-ABC123",),
            ).fetchone()
            assert material is not None, "Scanner DB should contain synced device"
            assert int(material[2] or 0) == int(student["id"])
            assert int(material[3] or 0) == 0

            assert int(student["book_loaned"] or 0) == 1
            assert int(student["device_loaned"] or 0) == 1

        # Idempotency check: re-import same payload and ensure no duplicates.
        payload_replay = os.path.join(admin_outbox, "reference_replay.json")
        _write_payload(payload_replay, payload)
        imported_again = database._scanner_import_admin_outbox(scanner_db, admin_outbox, archive_admin)
        assert imported_again == 1

        with sqlite3.connect(scanner_db) as conn:
            counts = conn.execute(
                "SELECT (SELECT COUNT(*) FROM books WHERE isbn = ?), (SELECT COUNT(*) FROM materials WHERE qr_code = ?)",
                ("1112223334", "MAT-000001-ABC123"),
            ).fetchone()
        assert int(counts[0] or 0) == 1, "Book import must be idempotent"
        assert int(counts[1] or 0) == 1, "Device import must be idempotent"
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    test_mailbox_inventory_sync_and_idempotency()
    print("✓ Mailbox inventory sync regression test passed")
