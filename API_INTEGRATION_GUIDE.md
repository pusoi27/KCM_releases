# Stdytime Integration API Guide

This guide is for external applications that need to:

1. **Read/copy Stdytime database data**
2. **Use Stdytime as an email bridge service**

---

## 1) Integration Modes

### Mode A: File-level DB copy (SQLite)
Use this mode when your app wants a full database snapshot for analytics, backup validation, or offline reporting.

### Mode B: HTTP Bridge API
Use this mode when your app wants controlled access to student exports and email send capabilities through Stdytime endpoints.

---

## 2) Database Integration (SQLite)

Stdytime stores data in a SQLite database (`Stdytime.db`). The runtime path is configurable in `db_config.json`.

### Primary database path resolution
Stdytime resolves DB path in this order:
1. `db_config.json` -> `db_path`
2. `DB_PATH` environment variable
3. runtime default (local app data path on installed Windows builds)

### Recommended external access pattern
Because Stdytime runs in **WAL mode** and may write frequently:

- Prefer a **read-only connection** for direct querying.
- If you need an external copy, use SQLite backup APIs (or equivalent safe snapshot flow), not raw file copy while writes are active.
- Treat Stdytime as the source of truth; avoid writing directly to its DB from external apps.

### Local/cloud storage concepts
From `db_config.json`:
- `db_path`: local fast runtime DB path
- `onedrive_sync_path`: cloud backup folder path (Stdytime manages `Stdytime.db` in that folder)
- `sync_interval_minutes`: currently fixed at 9 by app runtime

Example config template:

```json
{
  "db_path": "C:/Users/YourName/AppData/Local/StdyTime/Stdytime.db",
  "cloud_provider": "onedrive",
  "onedrive_sync_path": "C:/Users/YourName/OneDrive/StdyTime",
  "sync_interval_minutes": 9,
  "startup_pull_from_gdrive": false
}
```

### Core tables external apps usually read
- `students`
- `sessions`
- `staff`
- `assistant_sessions`
- `books`
- `materials`
- `app_license`
- `app_metadata`

---

## 3) HTTP Bridge API

Stdytime exposes two bridge endpoints intended for local trusted integrations.

### Base assumptions
- Runs in same local network context as Stdytime app host.
- Endpoint access is restricted to local/allowed hosts.
- Bearer token required.

### Security requirements
Your external app must send:

- `Authorization: Bearer <KCM_BRIDGE_TOKEN>`

And requests must originate from loopback/allowed hosts.

Environment keys:
- `KCM_BRIDGE_TOKEN`
- `KCM_BRIDGE_ALLOWED_HOSTS` (comma-separated; default includes `127.0.0.1`, `::1`, `localhost`)

If missing/invalid:
- `401` for invalid/missing token
- `403` for disallowed host

---

## 4) Endpoint: Export Students

`GET /api/students/export`

Returns a normalized student payload:

```json
{
  "students": [
    {
      "id": 7,
      "name": "Jane Doe",
      "email": "jane@example.com",
      "student_email": "jane@example.com",
      "phone": "555-1234",
      "guardian_name": "John Doe",
      "guardian": "John Doe",
      "active": 1,
      "subject": "S1,S2",
      "subjects_json": "[\"S1\", \"S2\"]",
      "subjects": ["S1", "S2"],
      "classification": "monitored",
      "el": 0,
      "pi": 1,
      "v": 0
    }
  ],
  "count": 1
}
```

Common responses:
- `200`: success
- `401`: unauthorized token
- `403`: host not allowed
- `500`: DB/internal error

---

## 5) Endpoint: Send Email

`POST /api/email/send`

Content-Type: `application/json`

### Request body
- `to` (string, required)
- `subject` (string, required)
- `body` (string, required)
- `html_body` (string, optional)
- `reply_to` (string, optional)
- `no_reply` (boolean, optional)
- `attachments` (array, optional)
  - each item:
    - `filename` (string, required)
    - `content_type` (string, optional; defaults to `application/octet-stream`)
    - `content_base64` (string, required)

### Example request

```json
{
  "to": "parent@example.com",
  "subject": "Bridge Test",
  "body": "Plain body",
  "html_body": "<p>HTML body</p>",
  "no_reply": true,
  "attachments": [
    {
      "filename": "sample.txt",
      "content_type": "text/plain",
      "content_base64": "dGVzdC1hdHRhY2htZW50LWJ5dGVz"
    }
  ]
}
```

### Success response

```json
{
  "success": true,
  "message": "Email sent successfully to parent@example.com"
}
```

### Error behavior
- `400`: validation errors (missing fields, bad attachment format/base64)
- `500`: SMTP config missing
- `502`: SMTP provider/runtime send errors

SMTP must be configured via:
- `SMTP_SERVER`
- `SMTP_PORT`
- `SENDER_EMAIL`
- `SENDER_PASSWORD`

---

## 6) Production Hardening Recommendations

1. Keep bridge endpoints local-only unless you add stronger perimeter controls.
2. Use a long random `KCM_BRIDGE_TOKEN`, rotate periodically.
3. Never commit `.env` with real secrets.
4. Log outbound email attempts in your external system for auditability.
5. Implement retry with exponential backoff on `502` email responses.
6. For DB copy workflows, avoid direct writes to Stdytime tables.

---

## 7) Quick Validation Checklist

- [ ] `.env` has bridge + SMTP values
- [ ] Stdytime server is running
- [ ] External app sends Bearer token
- [ ] Requests come from allowed host/IP
- [ ] `/api/students/export` returns expected schema
- [ ] `/api/email/send` can send a plain + HTML test message

---

## 8) Source of Truth in Codebase

For maintainers, the current integration behavior is implemented in:
- `routes/api.py` (bridge endpoints and payload contracts)
- `modules/database.py` (DB path resolution, schema creation, cloud sync behavior)
- `modules/email_manager.py` (SMTP send implementation)
- `routes/setup.py` (manual cloud push/pull setup flows)
