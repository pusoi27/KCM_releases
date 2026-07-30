#!/usr/bin/env python3
"""Simulate a user-initiated software update flow via Flask routes.

This script exercises:
- GET /sw-update/install (starts background worker)
- GET /api/sw-update/status (polling updates)

It monkeypatches update internals so no real download/copy/restart happens.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


# Keep deterministic startup behavior similar to existing validation script.
os.environ.setdefault("DEV_LICENSE_BYPASS", "true")
os.environ.setdefault("FLASK_USE_RELOADER", "true")
os.environ.setdefault("WERKZEUG_RUN_MAIN", "false")
os.environ.setdefault("ENABLE_PUBLIC_EXIT_ROUTE", "false")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _patch_runtime_guards(app_module):
    from modules import ls_license as ls_license_module

    app_module._POST_LAUNCH_STARTUP_DONE.set()
    app_module._has_configured_instructor_hours = lambda: True
    app_module.get_db_config_status = lambda: {"is_ready": True}
    app_module.user_identity_manager.get_saved_email = lambda: "dev@localhost"
    app_module.user_identity_manager.resolve_active_email = lambda _=None: "dev@localhost"
    app_module.user_identity_manager.sync_instructor_profile_email = lambda _email: None
    app_module.user_identity_manager.enforce_email_owner_signature = (
        lambda _email: {"ok": True, "action": "none", "message": ""}
    )
    ls_license_module.validate_email_matches_license = lambda _email: ""


def main() -> int:
    import app as app_module
    import routes.sw_update as sw_update

    _patch_runtime_guards(app_module)
    flask_app = app_module.app
    flask_app.testing = True

    simulated_payload_root = Path(tempfile.mkdtemp(prefix="stdytime_sw_sim_payload_"))
    (simulated_payload_root / "VERSION").write_text("99.99.999", encoding="utf-8")
    (simulated_payload_root / "app.py").write_text("# simulated payload\n", encoding="utf-8")

    def fake_check_for_update(current_version: str, identity: dict) -> dict:
        return {
            "current_version": current_version,
            "repo_url": "https://example.invalid/sw-gateway",
            "source": "gateway",
            "update_available": True,
            "latest_version": "99.99.999",
            "asset_name": "stdytime_installer_v99_99_999.zip",
            "download_url": "https://example.invalid/fake-download.zip",
            "download_headers": {},
            "checksum_url": "",
            "expected_sha256": "",
            "signature_url": "",
            "expected_signature": "",
            "minisign_public_key": "",
            "ticket_endpoint": "",
            "release_id": "v99.99.999",
            "raw": {},
        }

    def fake_download_release_zip(*_args, **_kwargs):
        return simulated_payload_root

    launched = {"called": False}

    def fake_launch_update_helper(source_dir, install_dir, wait_pid):
        launched["called"] = True
        print(f"[SIM] launch helper called")
        print(f"[SIM] source_dir={source_dir}")
        print(f"[SIM] install_dir={install_dir}")
        print(f"[SIM] wait_pid={wait_pid}")

    exit_calls: list[int] = []

    def fake_exit(code: int):
        exit_calls.append(int(code))

    sw_update._check_for_update = fake_check_for_update
    sw_update._download_release_zip = fake_download_release_zip
    sw_update._launch_update_helper = fake_launch_update_helper
    sw_update.os._exit = fake_exit

    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["user_email"] = "dev@localhost"

        install_resp = client.get("/sw-update/install", follow_redirects=False)
        print(f"[SIM] GET /sw-update/install -> {install_resp.status_code}")
        if install_resp.status_code in (301, 302, 303, 307, 308):
            print(f"[SIM] install redirect location: {install_resp.headers.get('Location')}")

        terminal_statuses = {"restarting", "error", "idle"}
        seen = []
        final_state = None

        for attempt in range(1, 31):
            status_resp = client.get("/api/sw-update/status")
            if status_resp.status_code != 200:
                print(f"[SIM] GET /api/sw-update/status -> {status_resp.status_code}")
                body_preview = status_resp.get_data(as_text=True)
                print(f"[SIM] status body: {body_preview[:400]}")
                return 2

            state = status_resp.get_json() or {}
            seen.append(state.get("status"))
            print(
                f"[SIM] poll {attempt:02d}: status={state.get('status')} "
                f"message={state.get('message')} error={state.get('error')}"
            )

            if state.get("status") in terminal_statuses:
                final_state = state
                break

            time.sleep(0.2)

        if final_state is None:
            print("[SIM] Timed out waiting for terminal update state.")
            return 3

        if final_state.get("status") != "restarting":
            print(f"[SIM] Expected 'restarting', got '{final_state.get('status')}'.")
            return 4

        if not launched["called"]:
            print("[SIM] launch helper was not called.")
            return 5

        for _ in range(15):
            if exit_calls:
                break
            time.sleep(0.2)

        if exit_calls != [0]:
            print(f"[SIM] expected one os._exit(0) call, got {exit_calls}.")
            return 6

        print("[SIM] User-initiated SW update simulation PASSED.")
        print(f"[SIM] observed statuses: {seen}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
