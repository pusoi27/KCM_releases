# Stdytime Plugin SDK (Phase C scaffold)

Plugins are local Python modules loaded from `plugins/<plugin-folder>/plugin.json`.

## Manifest (`plugin.json`)

```json
{
  "id": "example_echo",
  "name": "Example Echo Plugin",
  "version": "0.1.0",
  "enabled": true,
  "module": "plugins.example_echo.plugin",
  "hooks": ["before_email_send", "after_email_send"]
}
```

## Supported hooks

- `before_email_send(payload: dict) -> dict`
  - Called before integration email dispatch.
  - Return the same or modified payload.
- `after_email_send(payload: dict) -> dict`
  - Called after email dispatch with request+result payload.

## Notes

- Keep plugins local to the same machine as `stdytime`.
- Integration API enforces localhost + instructor station + HWID match.
- Any plugin exceptions are isolated and won’t crash the app.
