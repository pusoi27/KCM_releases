#!/usr/bin/env python3
"""Regression coverage for VCF student import corner cases."""

from __future__ import annotations

import os
import sqlite3
import tempfile

from modules import student_manager
from modules import database
from routes.students import _parse_vcf_contacts


SAMPLE_VCF = """BEGIN:VCARD
VERSION:3.0
FN:Ja'Sean & Journi & Jail'a Osceola (Jessalynn osceola)
EMAIL;TYPE=INTERNET:guardian@example.com
TEL;TYPE=CELL:555-123-4567
END:VCARD
"""

NOTE_SUBJECT_VCF = (
    "BEGIN:VCARD\n"
    "VERSION:3.0\n"
    "FN:Allison Walker (Precious Walker)\n"
    "EMAIL;TYPE=INTERNET:preciousgraham18@yahoo.com\n"
    "TEL;TYPE=CELL:(561) 335-0298\n"
    "NOTE:Allison Walker: Grade: 3\\, Subject: Rea\\nNotes: Her reading is low\n"
    "END:VCARD\n"
)


def _create_student_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                student_identifier TEXT DEFAULT '',
                subject TEXT,
                subjects_json TEXT DEFAULT '[]',
                subject_minutes_json TEXT DEFAULT '[]',
                total_study_minutes INTEGER DEFAULT 30,
                email TEXT,
                phone TEXT,
                guardian TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                book_loaned INTEGER DEFAULT 0,
                el INTEGER DEFAULT 0,
                pi INTEGER DEFAULT 0,
                v INTEGER DEFAULT 0,
                ind INTEGER DEFAULT 0,
                day1 TEXT DEFAULT '',
                day2 TEXT DEFAULT '',
                day1_time TEXT DEFAULT '',
                day2_time TEXT DEFAULT '',
                day3 TEXT DEFAULT '',
                day3_time TEXT DEFAULT '',
                day4 TEXT DEFAULT '',
                day4_time TEXT DEFAULT '',
                day5 TEXT DEFAULT '',
                day5_time TEXT DEFAULT '',
                day6 TEXT DEFAULT '',
                day6_time TEXT DEFAULT '',
                schedule_json TEXT DEFAULT '',
                qr_code BLOB,
                photo_blob BLOB,
                photo_mime TEXT DEFAULT ''
            )
            """
        )
        conn.commit()


def test_vcf_multi_first_name_expands_to_unique_students():
    contacts = _parse_vcf_contacts(SAMPLE_VCF)

    assert len(contacts) == 3
    assert [c["student_name"] for c in contacts] == [
        "Ja'Sean O.",
        "Journi O.",
        "Jail'a O.",
    ]
    assert all(c["guardian"] == "Jessalynn osceola" for c in contacts)
    assert all(c["email"] == "guardian@example.com" for c in contacts)
    assert all(c["phone"] == "555-123-4567" for c in contacts)
    assert all(c["match_on_email"] is False for c in contacts)
    assert all(c["match_on_identifier"] is False for c in contacts)

    tmp_dir = tempfile.mkdtemp(prefix="stdytime_vcf_test_")
    try:
        db_path = os.path.join(tmp_dir, "students.db")
        _create_student_table(db_path)

        original_module_db_path = student_manager.DB_PATH
        original_database_db_path = database.DB_PATH
        student_manager.DB_PATH = db_path
        database.DB_PATH = db_path
        try:
            for contact in contacts:
                result = student_manager.upsert_student_from_vcf_contact(
                    student_name=contact["student_name"],
                    email=contact["email"],
                    phone=contact["phone"],
                    guardian=contact["guardian"],
                    student_identifier=contact["student_identifier"],
                    match_on_email=contact["match_on_email"],
                    match_on_identifier=contact["match_on_identifier"],
                )
                assert result["action"] == "added"

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT name, email, guardian FROM students ORDER BY name"
                ).fetchall()

            assert {row[0] for row in rows} == {"Ja'Sean O.", "Journi O.", "Jail'a O."}
            assert len(rows) == 3
            assert all(row[1] == "guardian@example.com" for row in rows)
            assert all(row[2] == "Jessalynn osceola" for row in rows)
        finally:
            student_manager.DB_PATH = original_module_db_path
            database.DB_PATH = original_database_db_path
    finally:
        try:
            for root, dirs, files in os.walk(tmp_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(tmp_dir)
        except Exception:
            pass


def test_vcf_note_subject_is_mapped_to_subject_fields():
    contacts = _parse_vcf_contacts(NOTE_SUBJECT_VCF)

    assert len(contacts) == 1
    contact = contacts[0]
    assert contact["student_name"] == "Allison W."
    assert contact["guardian"] == "Precious Walker"
    assert contact["subjects"] == ["Reading"]
    assert contact["email"] == "preciousgraham18@yahoo.com"
    assert contact["phone"] == "(561) 335-0298"

    tmp_dir = tempfile.mkdtemp(prefix="stdytime_vcf_subject_test_")
    try:
        db_path = os.path.join(tmp_dir, "students.db")
        _create_student_table(db_path)

        original_module_db_path = student_manager.DB_PATH
        original_database_db_path = database.DB_PATH
        student_manager.DB_PATH = db_path
        database.DB_PATH = db_path
        try:
            result = student_manager.upsert_student_from_vcf_contact(
                student_name=contact["student_name"],
                email=contact["email"],
                phone=contact["phone"],
                guardian=contact["guardian"],
                student_identifier=contact["student_identifier"],
                subjects=contact["subjects"],
                match_on_email=contact["match_on_email"],
                match_on_identifier=contact["match_on_identifier"],
            )
            assert result["action"] == "added"

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT name, subject, subjects_json, subject_minutes_json FROM students LIMIT 1"
                ).fetchone()

            assert row[0] == "Allison W."
            assert row[1] == "Reading"
            assert row[2] == '["Reading"]'
            assert row[3] == '[30]'
        finally:
            student_manager.DB_PATH = original_module_db_path
            database.DB_PATH = original_database_db_path
    finally:
        try:
            for root, dirs, files in os.walk(tmp_dir, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(tmp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    test_vcf_multi_first_name_expands_to_unique_students()
    test_vcf_note_subject_is_mapped_to_subject_fields()
    print("✓ VCF importer regression tests passed")