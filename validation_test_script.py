#!/usr/bin/env python3
"""
Validation Test Script
======================

Purpose:
- Provide a single regression smoke test covering all major app features.
- Auto-discover GET routes so newly added features are automatically included.
- Run safe API checks that should never produce server errors.

Usage:
    python validation_test_script.py
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable


# Keep validation deterministic and independent from local activation state.
os.environ.setdefault("DEV_LICENSE_BYPASS", "true")
# Keep startup version bump logic disabled in this script's import flow.
# app.py skips startup bump when FLASK_USE_RELOADER=true and WERKZEUG_RUN_MAIN!=true.
os.environ.setdefault("FLASK_USE_RELOADER", "true")
os.environ.setdefault("WERKZEUG_RUN_MAIN", "false")
os.environ.setdefault("ENABLE_PUBLIC_EXIT_ROUTE", "false")


@dataclass
class CheckResult:
    name: str
    ok: bool
    status_code: int | None = None
    detail: str = ""


def _patch_runtime_guards(app_module):
    """Bypass setup/license/email/startup gates for test-client validation."""
    # Startup gate
    app_module._POST_LAUNCH_STARTUP_DONE.set()

    # First-run setup gate
    app_module._has_configured_instructor_hours = lambda: True
    app_module.get_db_config_status = lambda: {"is_ready": True}

    # Email gate
    app_module.user_identity_manager.get_saved_email = lambda: "validation@test.local"
    app_module.user_identity_manager.resolve_active_email = lambda _=None: "validation@test.local"
    app_module.user_identity_manager.sync_instructor_profile_email = lambda _email: None


def _build_path_from_rule(rule_text: str) -> str:
    """Convert Flask rule with converters into a concrete test path."""
    replacements = {
        "int": "1",
        "float": "1.0",
        "path": "sample",
        "uuid": "00000000-0000-0000-0000-000000000000",
        "string": "sample",
    }

    def repl(match: re.Match[str]) -> str:
        converter = match.group(1)
        name = match.group(2)
        if converter:
            return replacements.get(converter, "sample")
        # No converter provided (<name>)
        if name and name.endswith("id"):
            return "1"
        return "sample"

    # Matches <converter:name> or <name>
    return re.sub(r"<(?:(\w+):)?(\w+)>", repl, rule_text)


def _is_allowed_get_status(status: int) -> bool:
    # Accept expected auth/setup redirects and not-found outcomes.
    return status in {200, 301, 302, 303, 307, 308, 401, 403, 404, 428}


def _iter_get_rules(flask_app) -> Iterable[tuple[str, str]]:
    """Yield (endpoint, concrete_path) for all GET routes except static assets."""
    ignored_prefixes = ("/static",)
    ignored_exact = {
        "/favicon.ico",
    }

    for rule in sorted(flask_app.url_map.iter_rules(), key=lambda r: r.rule):
        if "GET" not in rule.methods:
            continue
        if rule.rule in ignored_exact:
            continue
        if rule.rule.startswith(ignored_prefixes):
            continue

        yield rule.endpoint, _build_path_from_rule(rule.rule)


def run_validation() -> int:
    import app as app_module

    _patch_runtime_guards(app_module)
    flask_app = app_module.app
    flask_app.testing = True

    results: list[CheckResult] = []

    with flask_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user_id"] = 1
            sess["user_email"] = "validation@test.local"

        # 1) Auto-discovered GET route coverage (broad feature smoke test)
        for endpoint, path in _iter_get_rules(flask_app):
            name = f"GET {path} ({endpoint})"
            try:
                response = client.get(path, follow_redirects=False)
                status = response.status_code

                if status >= 500:
                    results.append(CheckResult(name=name, ok=False, status_code=status, detail="Server error"))
                    continue

                if not _is_allowed_get_status(status):
                    results.append(
                        CheckResult(
                            name=name,
                            ok=False,
                            status_code=status,
                            detail="Unexpected status code",
                        )
                    )
                    continue

                results.append(CheckResult(name=name, ok=True, status_code=status))
            except Exception as exc:
                results.append(CheckResult(name=name, ok=False, detail=f"Exception: {exc}"))

        # 2) Targeted non-destructive API checks (write endpoints with invalid payloads)
        api_checks = [
            ("POST /api/books/loan invalid payload", "/api/books/loan", {}),
            ("POST /api/books/return invalid payload", "/api/books/return", {"book_id": -1}),
            ("POST /api/materials/loan invalid payload", "/api/materials/loan", {}),
            ("POST /api/materials/return invalid payload", "/api/materials/return", {"material_id": -1}),
            ("POST /api/schedule/assign invalid payload", "/api/schedule/assign", {}),
            ("POST /api/schedule/unassign invalid payload", "/api/schedule/unassign", {}),
            ("POST /api/schedule/mark-closed invalid payload", "/api/schedule/mark-closed", {}),
            ("POST /api/schedule/unmark-closed invalid payload", "/api/schedule/unmark-closed", {}),
        ]

        for name, path, payload in api_checks:
            try:
                response = client.post(path, json=payload)
                status = response.status_code

                if status >= 500:
                    results.append(CheckResult(name=name, ok=False, status_code=status, detail="Server error"))
                else:
                    results.append(CheckResult(name=name, ok=True, status_code=status))
            except Exception as exc:
                results.append(CheckResult(name=name, ok=False, detail=f"Exception: {exc}"))

    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print("=" * 88)
    print("VALIDATION TEST SCRIPT - REGRESSION SMOKE REPORT")
    print("=" * 88)
    print(f"Total checks : {len(results)}")
    print(f"Passed       : {len(passed)}")
    print(f"Failed       : {len(failed)}")
    print("=" * 88)

    if failed:
        print("\nFAILED CHECKS:")
        for item in failed:
            status_text = f" [{item.status_code}]" if item.status_code is not None else ""
            detail_text = f" - {item.detail}" if item.detail else ""
            print(f"  ✗ {item.name}{status_text}{detail_text}")
        print("\nValidation result: FAILED")
        return 1

    print("Validation result: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_validation())
