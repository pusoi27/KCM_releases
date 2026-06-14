# Stdytime — Integration & Plugin API

**Added in version 01.01.19 (Phase A/B) — UI in 01.01.20 (Phase C.1)**

This document covers how external local applications can connect to Stdytime to access student data, send emails, and extend behavior with plugins.

---

## Table of contents

1. [Security model](#1-security-model)
2. [Getting started — generating an API key](#2-getting-started--generating-an-api-key)
3. [Authentication headers](#3-authentication-headers)
4. [Integration API endpoints](#4-integration-api-endpoints)
   - [GET /integration/v1/students](#get-integrationv1students)
   - [POST /integration/v1/emails/send](#post-integrationv1emailssend)
   - [GET /integration/v1/plugins](#get-integrationv1plugins)
5. [Key management API](#5-key-management-api)
6. [Scope reference](#6-scope-reference)
7. [Rate limiting](#7-rate-limiting)
8. [Audit log](#8-audit-log)
9. [Plugin SDK](#9-plugin-sdk)
   - [Manifest format](#manifest-format)
   - [Hook reference](#hook-reference)
   - [Example plugin](#example-plugin)
10. [UI management page](#10-ui-management-page)
11. [Error reference](#11-error-reference)

---

## 1. Security model

Every request to the Integration API is validated through five independent layers:

| Layer | What is checked |
|---|---|
| **Localhost only** | `request.remote_addr` must be `127.0.0.1` / `::1`. Non-local requests are rejected with `403`. |
| **License valid** | The Stdytime instance must have an active license. |
| **Instructor Station** | Only the machine assigned the `instructor` role (or single-license installs) can use the Integration API. |
| **API key + HWID** | Every call carries a `Bearer` token and an `X-Client-HWID` header. The HWID in the header must match the local machine fingerprint and the fingerprint the key was bound to at creation time. |
| **Scope** | Each key carries a list of scopes. Calling an endpoint without the required scope returns `403`. |

Keys are stored **hashed + salted** (SHA-256) — the plaintext is shown once on creation and never again.

---

## 2. Getting started — generating an API key

### Via the UI (recommended)

1. In Stdytime, open **Utilities → Integration & Plugins** (`/integration/manage`).
2. Click **New Key**, fill in a name, select the scopes your app needs, set a rate limit, and click **Create Key**.
3. Copy the displayed key immediately — it is shown only once.
4. Note the **Machine HWID** displayed at the top of the page.

### Local key sharing with KCTM (same machine)

When a key is created with both scopes below, Stdytime will also write a local shared credentials bundle that KCTM can import automatically:

- `students:read`
- `emails:send`

Default path on Windows:

- `%LOCALAPPDATA%\\Stdytime\\integration\\kctm_integration_credentials.json`

Override path (Stdytime):

- `STDYTIME_SHARED_CREDENTIALS_PATH`

The shared bundle includes API key, bound HWID, and base URL for same-host bootstrap.

### Via the API (for automation)

```http
POST /integration/v1/keys
Content-Type: application/json
```

```json
{
  "name": "My Attendance Bridge",
  "scopes": ["students:read", "emails:send"],
  "rate_limit_per_minute": 120
}
```

This endpoint requires an active Stdytime browser session (cookie auth) on the same machine — it is not guarded by an API key.

**Response `201`:**

```json
{
  "id": 1,
  "name": "My Attendance Bridge",
  "api_key": "stk_...",
  "key_prefix": "stk_XXXXXXXXXX",
  "scopes": ["students:read", "emails:send"],
  "bound_hwid": "a1b2c3...",
  "rate_limit_per_minute": 120,
  "created_at": "2026-06-13T14:00:00+00:00"
}
```

---

## 3. Authentication headers

Every Integration API call must include **both** of the following headers:

```
Authorization: Bearer <your_api_key>
X-Client-HWID: <machine_hwid>
```

### Getting the HWID programmatically

```http
GET /integration/v1/hwid
```

Requires a Stdytime session (browser cookie). Returns:

```json
{ "hwid": "a1b2c3d4..." }
```

Or retrieve it from the UI page at **Utilities → Integration & Plugins**.

### Python example

```python
import requests

BASE = "http://127.0.0.1:5000"
API_KEY = "stk_your_key_here"
HWID = "a1b2c3d4..."   # from /integration/v1/hwid or the UI page

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "X-Client-HWID": HWID,
}

resp = requests.get(f"{BASE}/integration/v1/students", headers=HEADERS)
students = resp.json()
```

---

## 4. Integration API endpoints

All endpoints:
- Accept requests from `127.0.0.1` only
- Require `Authorization: Bearer <key>` + `X-Client-HWID: <hwid>`
- Return `Content-Type: application/json`

---

### `GET /integration/v1/students`

**Scope required:** `students:read`

Returns the active student list. Pass `?include_inactive=true` to also include soft-deleted (inactive) students.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `include_inactive` | bool | `false` | Include inactive / deleted students |

**Response `200`**

```json
[
  {
    "id": 42,
    "name": "Alice Johnson",
    "student_identifier": "AJ001",
    "email": "alice@example.com",
    "phone": "555-0100",
    "guardian": "Bob Johnson",
    "subjects": ["Math", "Reading"],
    "classification": "Monitored",
    "active": true,
    "checkout_notify_enabled": true,
    "schedule": [
      { "day": "Monday", "time": "15:00" },
      { "day": "Wednesday", "time": "15:00" }
    ],
    "photo_url": "/students/photo/42"
  }
]
```

---

### `POST /integration/v1/emails/send`

**Scope required:** `emails:send`

Sends an email through Stdytime's configured SMTP settings. The branded email shell is applied automatically unless `use_brand_shell` is set to `false` or you provide your own `html_body`.

**Request body**

```json
{
  "recipient_email": "parent@example.com",
  "subject": "Attendance update",
  "body": "Dear Parent,\n\nAlice attended class today.",
  "html_body": "<p>Optional HTML override</p>",
  "use_brand_shell": true,
  "metadata": { "student_id": 42 }
}
```

| Field | Required | Description |
|---|---|---|
| `recipient_email` | ✅ | Valid email address |
| `subject` | ✅ | Email subject line |
| `body` | ✅ | Plain-text body |
| `html_body` | optional | HTML body; overrides brand shell if provided |
| `use_brand_shell` | optional, default `true` | Wrap plain body in Stdytime's green branded shell |
| `metadata` | optional | Arbitrary key/value dict passed through plugin hooks |

**Response `200` (success)**

```json
{ "success": true, "message": "Email sent successfully to parent@example.com" }
```

**Response `502` (SMTP failure)**

```json
{ "success": false, "error": "SMTP error: ..." }
```

---

### `GET /integration/v1/plugins`

**Scope required:** `plugins:read`

Returns the list of installed plugins and their current load status.

**Response `200`**

```json
[
  {
    "id": "example_echo",
    "name": "Example Echo Plugin",
    "version": "0.1.0",
    "enabled": true,
    "module": "plugins.example_echo.plugin",
    "hooks": ["before_email_send", "after_email_send"],
    "status": "loaded",
    "error": ""
  }
]
```

Possible `status` values: `loaded`, `disabled`, `error`, `invalid`.

---

## 5. Key management API

These endpoints use Stdytime's browser session auth (same as the UI pages). They do **not** require an API key or `X-Client-HWID`.

| Method | Path | Description |
|---|---|---|
| `GET` | `/integration/v1/keys` | List all keys (metadata only, no plaintext) |
| `POST` | `/integration/v1/keys` | Create a new key (returns plaintext once) |
| `DELETE` | `/integration/v1/keys/<id>` | Revoke a key by ID |
| `GET` | `/integration/v1/hwid` | Return this machine's HWID |

All endpoints return `403` if called from a non-local address or from a Check-In Station.

---

## 6. Scope reference

| Scope | Grants access to |
|---|---|
| `students:read` | `GET /integration/v1/students` |
| `emails:send` | `POST /integration/v1/emails/send` |
| `plugins:read` | `GET /integration/v1/plugins` |
| `keys:manage` | Reserved for future programmatic key management |

Assign only the scopes an app actually needs. Scopes are checked on every request and cannot be elevated post-creation.

---

## 7. Rate limiting

Each API key has an independent per-minute request bucket (default: **120 req/min**). The window resets every 60 seconds.

When the limit is exceeded the API returns:

```
HTTP 429
{ "error": "Rate limit exceeded." }
```

Set the limit when creating a key:

```json
{ "name": "...", "scopes": [...], "rate_limit_per_minute": 60 }
```

Valid range: 10 – 600 requests per minute.

---

## 8. Audit log

Every integration call is recorded in the `integration_audit_log` SQLite table and displayed in the UI under **Integration & Plugins → Recent Audit Log**.

Logged fields: timestamp, key prefix, action, HTTP method, path, remote address, status code, success flag, error message.

The log is viewable at `/integration/manage` and is not exposed over the API.

---

## 9. Plugin SDK

Plugins are Python modules in the `plugins/` folder. They are loaded at startup and can intercept integration email flow via hook functions.

### Manifest format

Each plugin lives in `plugins/<plugin-folder>/` and must contain a `plugin.json` file:

```json
{
  "id": "my_plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "enabled": true,
  "module": "plugins.my_plugin.plugin",
  "hooks": [
    "before_email_send",
    "after_email_send"
  ]
}
```

| Field | Required | Description |
|---|---|---|
| `id` | ✅ | Unique string identifier |
| `name` | ✅ | Human-readable display name |
| `version` | optional | Semver string |
| `enabled` | optional, default `true` | Set to `false` to disable without removing |
| `module` | ✅ | Python dotted import path |
| `hooks` | ✅ | List of hook function names to register |

### Hook reference

#### `before_email_send(payload: dict) → dict`

Called before an integration email is dispatched. Receives and must return the payload dict.

| Key in payload | Description |
|---|---|
| `recipient_email` | Destination address |
| `subject` | Email subject |
| `body` | Plain-text body |
| `html_body` | HTML body (may be empty) |
| `center_name` | Resolved center name |
| `metadata` | Caller-supplied arbitrary dict |

Return a modified dict to change any field before sending.

#### `after_email_send(payload: dict) → dict`

Called after dispatch. Payload contains:

| Key | Description |
|---|---|
| `request` | The payload that was sent |
| `result` | SMTP result: `{"success": bool, "message"|"error": str}` |

Use for local analytics, logging, or bridging to another system. Return value is ignored.

### Example plugin

```python
# plugins/my_plugin/plugin.py

def before_email_send(payload):
    payload = dict(payload)
    payload["subject"] = "[My App] " + payload.get("subject", "")
    return payload

def after_email_send(payload):
    result = payload.get("result", {})
    if result.get("success"):
        print("[my_plugin] email sent to", payload["request"]["recipient_email"])
    return payload
```

```json
// plugins/my_plugin/plugin.json
{
  "id": "my_plugin",
  "name": "My Plugin",
  "version": "1.0.0",
  "enabled": true,
  "module": "plugins.my_plugin.plugin",
  "hooks": ["before_email_send", "after_email_send"]
}
```

Restart Stdytime after adding or modifying a plugin for changes to take effect.

---

## 10. UI management page

Navigate to **Utilities → Integration & Plugins** in the Stdytime navbar.

The page is available only when:
- The browser is accessing from `127.0.0.1`
- The machine has a valid license
- The station role is `instructor` (or single-license)

### Sections

**Machine HWID** — displays and lets you copy the HWID needed by external apps.

**API Keys** — full key lifecycle management:
- Create a key with a name, scopes, and rate limit
- Plaintext key shown once in a copy-ready banner immediately after creation
- Revoke any active key with confirmation

**Installed Plugins** — shows all plugins in `plugins/` with their load status (Loaded / Disabled / Error) and registered hooks.

**Recent Audit Log** — last 50 integration events with method, path, status code, and any error.

---

## 11. Error reference

| HTTP | Body `error` | Cause |
|---|---|---|
| `401` | `Missing bearer API key.` | No `Authorization` header |
| `401` | `Invalid integration API key.` | Key not found or hash mismatch |
| `401` | `Missing X-Client-HWID header.` | Header absent |
| `403` | `Integration API accepts local requests only.` | Non-loopback source address |
| `403` | `A valid license is required.` | Stdytime license not active |
| `403` | `Integration API is available only on Instructor Station.` | Machine role is `checkin` |
| `403` | `HWID mismatch.` | `X-Client-HWID` doesn't match machine or key binding |
| `403` | `Missing required scope.` + `missing_scopes` array | Key lacks the needed scope |
| `429` | `Rate limit exceeded.` | Per-minute bucket exhausted |
| `400` | field-specific message | Validation error in request body |
| `502` | SMTP error string | Email dispatch failed |
