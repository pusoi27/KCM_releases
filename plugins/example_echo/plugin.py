"""Example plugin hooks for integration email flow."""

from __future__ import annotations

from typing import Any


def before_email_send(payload: dict[str, Any]) -> dict[str, Any]:
    data = dict(payload or {})
    subject = str(data.get("subject") or "").strip()
    if subject and not subject.startswith("[Local Integration]"):
        data["subject"] = f"[Local Integration] {subject}"
    return data


def after_email_send(payload: dict[str, Any]) -> dict[str, Any]:
    # Hook for local side effects if needed (analytics/logging/bridge calls).
    return dict(payload or {})
