#!/usr/bin/env python3
"""
Regression test for mailbox-based app_license sharing:
- Shared license fields must sync from admin to scanner.
- Machine-local fields (station_role, machine_fingerprint) must be preserved.
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
            CREATE TABLE IF NOT EXISTS app_license (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                license_key TEXT,
                licensee TEXT,
                email TEXT,
                issued_at TEXT,
                expires_at TEXT,
                machine_fingerprint TEXT,
                metadata_json TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                ls_instance_id TEXT DEFAULT '',
                ls_status TEXT DEFAULT '',
                ls_last_verified_at TEXT DEFAULT '',
                activation_limit INTEGER DEFAULT 0,
                activation_usage INTEGER DEFAULT 0,
                station_role TEXT DEFAULT ''
            )
            """
        )
        conn.commit()


def _seed_admin(admin_db: str) -> None:
    with sqlite3.connect(admin_db) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO staff (name, role, email, phone, loading) VALUES (?, ?, ?, ?, ?)",
            ("Admin User", "Admin", "admin@example.com", "555-1000", 1),
        )
        cur.execute(
            """
            INSERT INTO app_license (
                id, license_key, licensee, email, issued_at, expires_at,
                machine_fingerprint, metadata_json, ls_instance_id, ls_status,
                ls_last_verified_at, activation_limit, activation_usage, station_role
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "LS-ADMIN-KEY-123",
                "Kumo Parent",
                "license@example.com",
                "2026-01-01",
                "2027-01-01",
                "admin-machine-fp",
                json.dumps({"source": "admin", "plan": "pro"}),
                "inst_abc123",
                "active",
                "2026-06-11T12:00:00+00:00",
                2,
                1,
                "instructor",
            ),
        )
        conn.commit()


def _seed_scanner(scanner_db: str) -> None:
    with sqlite3.connect(scanner_db) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_license (id, machine_fingerprint, station_role, activation_limit, activation_usage) VALUES (?, ?, ?, ?, ?)",
            (1, "scanner-machine-fp", "checkin", 0, 0),
        )
        conn.commit()


def test_mailbox_app_license_shared_fields_sync_preserve_machine_local() -> None:
    tmp = tempfile.mkdtemp(prefix="stdytime_mailbox_license_test_")
    try:
        admin_db = os.path.join(tmp, "admin.db")
        scanner_db = os.path.join(tmp, "scanner.db")
        admin_outbox = os.path.join(tmp, "Admin_Outbox")
        archive_admin = os.path.join(tmp, "Archive", "Admin_Outbox")
        os.makedirs(admin_outbox, exist_ok=True)
        os.makedirs(archive_admin, exist_ok=True)

        _create_test_db(admin_db)
        _create_test_db(scanner_db)
        _seed_admin(admin_db)
        _seed_scanner(scanner_db)

        exported = database._admin_export_staff_snapshot(admin_db, admin_outbox)
        assert exported is True

        payload_files = [
            os.path.join(admin_outbox, name)
            for name in sorted(os.listdir(admin_outbox))
            if name.lower().endswith(".json")
        ]
        assert payload_files, "Expected at least one Admin_Outbox payload"
        with open(payload_files[0], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        license_payload = payload.get("app_license") or {}
        assert str(license_payload.get("license_key") or "") == "LS-ADMIN-KEY-123", f"unexpected payload app_license: {license_payload}"

        imported_count = database._scanner_import_admin_outbox(scanner_db, admin_outbox, archive_admin)
        assert imported_count == 1, f"Expected one payload imported, got {imported_count}"

        with sqlite3.connect(scanner_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM app_license WHERE id = 1 LIMIT 1").fetchone()

        assert row is not None, "Scanner app_license row should exist"

        # Shared fields should match admin snapshot.
        assert str(row["license_key"] or "") == "LS-ADMIN-KEY-123", f"license_key mismatch: {dict(row)}"
        assert str(row["licensee"] or "") == "Kumo Parent", f"licensee mismatch: {dict(row)}"
        assert str(row["email"] or "") == "license@example.com", f"email mismatch: {dict(row)}"
        assert str(row["issued_at"] or "") == "2026-01-01", f"issued_at mismatch: {dict(row)}"
        assert str(row["expires_at"] or "") == "2027-01-01", f"expires_at mismatch: {dict(row)}"
        assert str(row["ls_instance_id"] or "") == "inst_abc123", f"ls_instance_id mismatch: {dict(row)}"
        assert str(row["ls_status"] or "") == "active", f"ls_status mismatch: {dict(row)}"
        assert int(row["activation_limit"] or 0) == 2, f"activation_limit mismatch: {dict(row)}"
        assert int(row["activation_usage"] or 0) == 1, f"activation_usage mismatch: {dict(row)}"

        # Machine-local fields must remain scanner-local.
        assert str(row["machine_fingerprint"] or "") == "scanner-machine-fp"
        assert str(row["station_role"] or "") == "checkin"

    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


def test_default_high_activation_limit_does_not_force_station_role_selection() -> None:
    tmp = tempfile.mkdtemp(prefix="stdytime_default_limit_test_")
    try:
        db_path = os.path.join(tmp, "license_defaults.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_license (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    license_key TEXT,
                    licensee TEXT,
                    email TEXT,
                    issued_at TEXT,
                    expires_at TEXT,
                    machine_fingerprint TEXT,
                    metadata_json TEXT DEFAULT '{}',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    ls_instance_id TEXT DEFAULT '',
                    ls_status TEXT DEFAULT '',
                    ls_last_verified_at TEXT DEFAULT '',
                    activation_limit INTEGER DEFAULT 0,
                    activation_usage INTEGER DEFAULT 0,
                    station_role TEXT DEFAULT ''
                )
                """
            )
            conn.execute(
                "INSERT INTO app_license (id, license_key, ls_instance_id, ls_status, activation_limit, station_role) VALUES (?, ?, ?, ?, ?, ?)",
                (1, "DEFAULT-LIMIT-KEY", "inst_default", "active", 3, ""),
            )
            conn.commit()

        old_db = database.DB_PATH
        database.DB_PATH = db_path
        try:
            assert database.is_station_mailbox_mode_enabled() is False
            assert __import__("modules.ls_license", fromlist=["requires_station_role_selection"]).requires_station_role_selection() is False
        finally:
            database.DB_PATH = old_db
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


def test_single_station_role_is_valid_selection() -> None:
    import modules.ls_license as ls_license

    row = {"ls_instance_id": "inst_single", "station_role": ""}
    old_get_row = ls_license._get_ls_row
    old_connect = ls_license.sqlite3.connect
    ls_license._get_ls_row = lambda: row
    ls_license.sqlite3.connect = lambda *args, **kwargs: old_connect(*args, **kwargs)
    try:
        ok, msg = ls_license.set_station_role("single")
        assert ok is True, msg
        row["station_role"] = "single"
        assert ls_license.get_station_role() == "single"
    finally:
        ls_license._get_ls_row = old_get_row
        ls_license.sqlite3.connect = old_connect


if __name__ == "__main__":
    test_mailbox_app_license_shared_fields_sync_preserve_machine_local()
    test_default_high_activation_limit_does_not_force_station_role_selection()
    test_single_station_role_is_valid_selection()
    print("✓ Mailbox app_license sync regression test passed")
