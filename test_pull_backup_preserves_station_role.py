#!/usr/bin/env python3
"""
Regression test: manual cloud pull must not overwrite machine-local station role.
"""

import os
import shutil
import sqlite3
import tempfile

from modules import database


def _create_license_db(path: str) -> None:
    with sqlite3.connect(path) as conn:
        cur = conn.cursor()
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


def _seed_local_scanner(local_db: str) -> None:
    with sqlite3.connect(local_db) as conn:
        conn.execute(
            """
            INSERT INTO app_license (
                id, license_key, licensee, email,
                machine_fingerprint, station_role,
                activation_limit, activation_usage, ls_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "LOCAL-OLD-KEY",
                "Local User",
                "local@example.com",
                "scanner-fingerprint",
                "checkin",
                2,
                1,
                "active",
            ),
        )
        conn.commit()


def _seed_cloud_instructor(cloud_db: str) -> None:
    with sqlite3.connect(cloud_db) as conn:
        conn.execute(
            """
            INSERT INTO app_license (
                id, license_key, licensee, email, issued_at, expires_at,
                machine_fingerprint, ls_instance_id, ls_status,
                ls_last_verified_at, activation_limit, activation_usage, station_role
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "CLOUD-LS-KEY-999",
                "Cloud Licensee",
                "cloud-license@example.com",
                "2026-01-01",
                "2027-01-01",
                "instructor-fingerprint",
                "inst_cloud_999",
                "active",
                "2026-06-11T12:30:00+00:00",
                2,
                2,
                "instructor",
            ),
        )
        conn.commit()


def test_pull_backup_preserves_local_station_role_and_fingerprint() -> None:
    tmp = tempfile.mkdtemp(prefix="stdytime_pull_preserve_role_")
    try:
        local_db = os.path.join(tmp, "local_scanner.db")
        cloud_db = os.path.join(tmp, "cloud_backup.db")

        _create_license_db(local_db)
        _create_license_db(cloud_db)
        _seed_local_scanner(local_db)
        _seed_cloud_instructor(cloud_db)

        pulled = database.sync_from_gdrive(local_db, cloud_db, force=True)
        assert pulled is True, "Expected forced cloud pull to succeed"

        with sqlite3.connect(local_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM app_license WHERE id = 1 LIMIT 1").fetchone()

        assert row is not None, "Expected app_license row after pull"

        # Shared fields should now match cloud backup.
        assert str(row["license_key"] or "") == "CLOUD-LS-KEY-999"
        assert str(row["licensee"] or "") == "Cloud Licensee"
        assert str(row["email"] or "") == "cloud-license@example.com"
        assert str(row["ls_instance_id"] or "") == "inst_cloud_999"
        assert str(row["ls_status"] or "") == "active"

        # Machine-local fields must remain from scanner machine.
        assert str(row["station_role"] or "") == "checkin"
        assert str(row["machine_fingerprint"] or "") == "scanner-fingerprint"

    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


if __name__ == "__main__":
    test_pull_backup_preserves_local_station_role_and_fingerprint()
    print("✓ Pull-backup station-role preservation regression test passed")
