#!/usr/bin/env python3
"""Smoke tests for the KCM ↔ Stdytime email bridge."""

from __future__ import annotations

import base64
import os
import sys
from email import message_from_bytes
from unittest.mock import patch


os.environ.setdefault("KCM_BRIDGE_TOKEN", "bridge-test-token")
os.environ.setdefault("KCM_BRIDGE_ALLOWED_HOSTS", "127.0.0.1,::1")


def _bridge_headers() -> dict[str, str]:
    return {"Authorization": "Bearer bridge-test-token"}


def _student_row() -> list:
    row = [None] * 18
    row[0] = 7
    row[1] = "Jane Doe"
    row[2] = "S1,S2"
    row[3] = "jane@example.com"
    row[4] = "555-1234"
    row[5] = "John Doe"
    row[7] = 1
    row[10] = 0
    row[11] = 1
    row[12] = 0
    row[17] = '["S1", "S2"]'
    return row


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: int = 15):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent_messages = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, sender_email: str, sender_password: str):
        self.logged_in = (sender_email, sender_password)

    def send_message(self, msg):
        self.sent_messages.append(msg)


class _FakeSMTPSSL(_FakeSMTP):
    pass


def test_students_export_route() -> bool:
    print("\n=== TEST: /api/students/export ===")
    try:
        from app import app
        import routes.api as api_routes

        with app.test_client() as client:
            response = client.get(
                "/api/students/export",
                headers=_bridge_headers(),
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            if response.status_code == 500:
                print(f"✗ Unexpected server error: {response.get_data(as_text=True)[:200]}")
                return False

            if response.status_code == 200:
                data = response.get_json()
                students = data.get("students", []) if isinstance(data, dict) else []
                print(f"✓ Route returned {len(students)} student(s)")
                if students and students[0].get("guardian_name") == "John Doe":
                    print("✓ Bridge payload includes guardian_name")
                if students and students[0].get("student_email") == "jane@example.com":
                    print("✓ Bridge payload includes student_email alias")
                return True

            print(f"✗ Unexpected status code: {response.status_code}")
            return False
    except Exception as exc:
        print(f"✗ Error: {exc}")
        return False


def test_email_send_route() -> bool:
    print("\n=== TEST: /api/email/send ===")
    try:
        from app import app
        import routes.api as api_routes

        _FakeSMTP.instances.clear()
        fake_email_manager = type(
            "FakeEmailManager",
            (),
            {
                "sender_email": "noreply@stdytime.com",
                "sender_password": "app-password",
                "smtp_server": "smtp.example.com",
                "smtp_port": 587,
            },
        )()

        attachment_bytes = b"test-attachment-bytes"
        attachment_b64 = base64.b64encode(attachment_bytes).decode("ascii")

        with patch.object(api_routes, "get_email_manager", return_value=fake_email_manager), patch.object(
            api_routes.smtplib, "SMTP", _FakeSMTP
        ), patch.object(api_routes.smtplib, "SMTP_SSL", _FakeSMTPSSL):
            with app.test_client() as client:
                response = client.post(
                    "/api/email/send",
                    headers=_bridge_headers(),
                    environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                    json={
                        "to": "parent@example.com",
                        "subject": "Bridge Test",
                        "body": "Plain body",
                        "html_body": "<p>HTML body</p>",
                        "no_reply": True,
                        "attachments": [
                            {
                                "filename": "sample.txt",
                                "content_type": "text/plain",
                                "content_base64": attachment_b64,
                            }
                        ],
                    },
                )

        if response.status_code != 200:
            print(f"✗ Unexpected status code: {response.status_code}")
            print(response.get_data(as_text=True))
            return False

        payload = response.get_json() or {}
        if not payload.get("success"):
            print(f"✗ Bridge returned failure: {payload}")
            return False

        if not _FakeSMTP.instances:
            print("✗ SMTP stub was not invoked")
            return False

        smtp = _FakeSMTP.instances[0]
        if not smtp.started_tls:
            print("✗ Expected STARTTLS path for port 587")
            return False
        if smtp.logged_in != ("noreply@stdytime.com", "app-password"):
            print(f"✗ Unexpected login tuple: {smtp.logged_in}")
            return False
        if not smtp.sent_messages:
            print("✗ No message was sent")
            return False

        message = smtp.sent_messages[0]
        if message["Reply-To"] != "noreply@kcm.local":
            print(f"✗ Reply-To mismatch: {message['Reply-To']}")
            return False

        raw = message.as_bytes()
        parsed = message_from_bytes(raw)
        body_text = raw.decode("utf-8", errors="ignore")
        if "Plain body" not in body_text or "HTML body" not in body_text:
            print("✗ Message body missing expected content")
            return False
        if "sample.txt" not in body_text:
            print("✗ Attachment filename not found in MIME message")
            return False

        print("✓ Email bridge accepted payload and built MIME message")
        return True
    except Exception as exc:
        print(f"✗ Error: {exc}")
        return False


def test_auth_failures() -> bool:
    print("\n=== TEST: Bridge auth failures ===")
    try:
        from app import app

        with app.test_client() as client:
            no_token = client.get(
                "/api/students/export",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            wrong_host = client.get(
                "/api/students/export",
                headers=_bridge_headers(),
                environ_overrides={"REMOTE_ADDR": "10.0.0.5"},
            )

        if no_token.status_code != 401:
            print(f"✗ Expected 401 without token, got {no_token.status_code}")
            return False
        if wrong_host.status_code != 403:
            print(f"✗ Expected 403 for remote host, got {wrong_host.status_code}")
            return False

        print("✓ Auth and host restrictions behave as expected")
        return True
    except Exception as exc:
        print(f"✗ Error: {exc}")
        return False


def main() -> None:
    print("=" * 60)
    print("KCM ↔ STDYTIME EMAIL BRIDGE SMOKE TEST")
    print("=" * 60)

    results = [
        test_auth_failures(),
        test_students_export_route(),
        test_email_send_route(),
    ]

    print("\n" + "=" * 60)
    if all(results):
        print("✓ ALL BRIDGE TESTS PASSED")
    else:
        print("✗ SOME BRIDGE TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()