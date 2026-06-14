# THIRD_PARTY_INTEGRATION_SETTINGS

Use this one-page guide to configure third-party apps that connect to Stdytime.

## 1) Mandatory partner-side settings

Set these exact values in the partner application:

- `STDYTIME_BASE_URL=http://127.0.0.1:5000`
- `STDYTIME_BRIDGE_TOKEN=<exact value of Stdytime KCM_BRIDGE_TOKEN>`
- `STDYTIME_TIMEOUT_SECONDS=15`

Derived endpoint values:

- `STDYTIME_EXPORT_URL=${STDYTIME_BASE_URL}/api/students/export`
- `STDYTIME_EMAIL_URL=${STDYTIME_BASE_URL}/api/email/send`

Mandatory header for **both** endpoints:

- `Authorization: Bearer <STDYTIME_BRIDGE_TOKEN>`

## 2) Access control rules (must pass)

Stdytime bridge endpoints enforce two checks:

1. **Host/IP check** (local/allowed host only)
2. **Bearer token check** (`Authorization` header)

Default allowed hosts are loopback values:

- `127.0.0.1`
- `::1`
- `localhost`

If the caller runs on a different machine/IP, that host/IP must be added to Stdytime `KCM_BRIDGE_ALLOWED_HOSTS`.

## 3) Explicit contract: `GET /api/students/export`

### Purpose

Returns student records as normalized JSON for sync/integration.

### Request requirements

- Method: `GET`
- URL: `/api/students/export`
- Header: `Authorization: Bearer <token>`
- Caller host/IP must be allowed (see section 2)

### Response behavior

- `200 OK`: JSON payload with top-level keys:
  - `students` (array)
  - `count` (integer)
- `401 Unauthorized`: token missing or token does not match `KCM_BRIDGE_TOKEN`
- `403 Forbidden`: caller host/IP is not allowed
- `500 Internal Server Error`: database/runtime issue

### Success payload example

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

## 4) Explicit contract: `POST /api/email/send`

### Request requirements

- Method: `POST`
- URL: `/api/email/send`
- Headers:
  - `Authorization: Bearer <token>`
  - `Content-Type: application/json`

Required JSON fields:

- `to` (string)
- `subject` (string)
- `body` (string)

Optional JSON fields:

- `html_body` (string)
- `reply_to` (string)
- `no_reply` (boolean)
- `attachments` (array)
  - `filename` (required)
  - `content_base64` (required)
  - `content_type` (optional; default `application/octet-stream`)

### Minimal request body example

```json
{
  "to": "parent@example.com",
  "subject": "Bridge Test",
  "body": "Plain body",
  "html_body": "<p>HTML body</p>"
}
```

### Response behavior

- `200`: success (`{"success": true, ...}`)
- `400`: invalid payload
- `401`: missing/invalid token
- `403`: disallowed caller host/IP
- `500`: SMTP config/server-side configuration issue
- `502`: SMTP provider/send error

## 5) Database download clarification (very important)

There is currently **no HTTP endpoint that serves the raw `Stdytime.db` file**.

If a partner needs raw DB data, use one of these methods:

1. **Preferred for integrations:** use `GET /api/students/export`.
2. **Raw SQLite copy workflow on host machine:**
   - Resolve database path in this order:
     1. `db_config.json` → `db_path`
     2. `DB_PATH` environment variable
     3. Runtime default local app data path used by Stdytime
   - Create a safe snapshot (do not do naive live copy during writes).
   - If using file-level copy in WAL mode, copy all SQLite sidecar files as needed (`.db`, `.db-wal`, `.db-shm`) or use SQLite backup API.
   - Validate copied DB can be opened and queried before using it downstream.

## 6) Partner-side go-live checklist

- [ ] `STDYTIME_BASE_URL` points to the running Stdytime server
- [ ] `Authorization` header is exactly `Bearer <token>`
- [ ] Token exactly matches Stdytime `KCM_BRIDGE_TOKEN`
- [ ] Caller host/IP is in `KCM_BRIDGE_ALLOWED_HOSTS`
- [ ] `GET /api/students/export` returns `200` with `students` + `count`
- [ ] `POST /api/email/send` returns `200` with `{"success": true}`
- [ ] Team understands there is no raw DB HTTP download endpoint
