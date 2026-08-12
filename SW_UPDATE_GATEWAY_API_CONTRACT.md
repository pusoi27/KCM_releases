# Stdytime Private SW Update Gateway Contract

This document defines the HTTP contract expected by `routes/sw_update.py`.

## Goals
- No anonymous public update downloads.
- Update artifacts are delivered only after app + license/machine checks.
- Compatible with large ZIP artifacts (e.g., 80MB+ from private GitHub LFS origin).

## App request headers
The app sends these headers on update check and ticket requests:

- `X-Stdytime-App: stdytime`
- `X-Stdytime-Version: <current_version>`
- `X-Stdytime-Channel: stable|beta|canary`
- `X-Stdytime-Machine: <machine_fingerprint>`
- `X-Stdytime-Station-Role: instructor|checkin|""`
- `X-Stdytime-License-Status: valid|expired|...`
- `X-Stdytime-Activation-Limit: <int>`
- `Authorization: Bearer <SW_UPDATE_GATEWAY_TOKEN>` (optional)

If `SW_UPDATE_CLIENT_PROOF_SECRET` is set, app also sends:
- `X-Stdytime-Proof-Timestamp: <unix_seconds>`
- `X-Stdytime-Proof: <hmac_sha256(machine|version|channel|timestamp)>`

## 1) Check endpoint

### Request
`GET /updates/check?app=stdytime&channel=stable&current_version=01.03.148`

### Success response (no update)
```json
{
  "ok": true,
  "update_available": false,
  "latest_version": "01.03.148",
  "message": "Already up to date"
}
```

### Success response (update available)
```json
{
  "ok": true,
  "update_available": true,
  "latest_version": "01.03.149",
  "asset_name": "stdytime_installer_v01_03_149.zip",
  "release_id": "v01.03.149",
  "ticket_endpoint": "/updates/ticket",
  "checksum_url": "/updates/files/stdytime_installer_v01_03_149.zip.sha256"
}
```

Optional fast path: you may return `download_url` directly from `/updates/check`.

Optional integrity fields accepted by app on `/updates/check` and `/updates/ticket`:
- `expected_sha256` (64-char SHA-256 hex)
- `checksum_url` (URL to `.sha256` sidecar)

## 2) Ticket endpoint

### Request
`POST /updates/ticket`

Body example:
```json
{
  "app": "stdytime",
  "channel": "stable",
  "current_version": "01.03.148",
  "latest_version": "01.03.149",
  "asset_name": "stdytime_installer_v01_03_149.zip",
  "release_id": "v01.03.149",
  "machine_fingerprint": "...",
  "station_role": "instructor",
  "license_status": "valid"
}
```

### Success response
```json
{
  "ok": true,
  "download_url": "https://updates.yourdomain.com/updates/download?ticket=<opaque>",
  "download_headers": {
    "Authorization": "Bearer <short_lived_download_token>"
  },
  "checksum_url": "https://updates.yourdomain.com/updates/files/stdytime_installer_v01_03_149.zip.sha256"
}
```

Notes:
- `download_url` may be absolute or relative.
- `download_headers` is optional; app will attach any provided headers.

## Download endpoint expectations
The `download_url` target should:
- authorize ticket/token
- support large file streaming
- ideally support `Range` for resume/retry
- return the ZIP binary

## Security recommendations
- Ticket TTL: 60-180 seconds
- One-time ticket use
- Bind ticket to machine fingerprint + latest version
- Rate-limit per machine/IP
- Audit-log issued tickets and completed downloads
- Verify HMAC proof timestamp skew on gateway if proof headers are used
- Publish and verify `.sha256` for each release package

## Migration strategy
1. Deploy gateway with `/updates/check` and `/updates/ticket`.
2. Set in `.env`:
   - `SW_UPDATE_GATEWAY_URL=https://updates.yourdomain.com`
   - optional `SW_UPDATE_GATEWAY_TOKEN`
   - optional `SW_UPDATE_CLIENT_PROOF_SECRET`
3. Keep `SW_UPDATE_ALLOW_DIRECT_GITHUB_FALLBACK=false` in production.
