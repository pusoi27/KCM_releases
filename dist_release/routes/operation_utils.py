"""Shared helpers for route-level rollback, cache invalidation, and failure payloads."""

from __future__ import annotations

from typing import Callable, Iterable

from flask import flash, jsonify

from modules import db_backup_recovery


def invalidate_scoped_cache(*invalidators: Callable[[], object]) -> None:
    """Run one or more tenant-scoped cache invalidators."""
    for invalidator in invalidators:
        if invalidator is not None:
            invalidator()


def build_scoped_failure_message(backup_path: str, error: object, restore_error: object | None = None) -> str:
    """Build a standardized tenant-scoped rollback failure message."""
    if restore_error is None:
        return (
            f"Operation failed. Your account data was restored from backup only for your records. "
            f"Backup: {backup_path}. Error: {error}"
        )

    return (
        f"Operation failed and automatic restore for your account also failed. "
        f"Backup: {backup_path}. Error: {error}. Restore error: {restore_error}"
    )


def restore_scoped_state(
    backup_path: str,
    table_names: Iterable[str],
    *invalidators: Callable[[], object],
) -> Exception | None:
    """Restore tenant-scoped rows from backup and invalidate only that tenant's caches.

    Returns None on successful restore, otherwise returns the restore exception.
    Cache invalidation always runs.
    """
    restore_error: Exception | None = None
    try:
        db_backup_recovery.restore_tenant_rows(backup_path, *table_names)
    except Exception as exc:  # pragma: no cover - exceptional path exercised via routes
        restore_error = exc
    finally:
        invalidate_scoped_cache(*invalidators)
    return restore_error


def flash_scoped_failure(
    *,
    backup_path: str,
    table_names: Iterable[str],
    error: object,
    category: str = "danger",
    invalidators: Iterable[Callable[[], object]] = (),
) -> Exception | None:
    """Restore tenant-scoped state and flash a standardized failure message."""
    restore_error = restore_scoped_state(backup_path, table_names, *invalidators)
    flash(build_scoped_failure_message(backup_path, error, restore_error), category)
    return restore_error


def json_scoped_failure(
    *,
    backup_path: str,
    table_names: Iterable[str],
    error: object,
    invalidators: Iterable[Callable[[], object]] = (),
    status_code: int = 500,
    extra_payload: dict | None = None,
):
    """Restore tenant-scoped state and return a standardized JSON failure response."""
    restore_error = restore_scoped_state(backup_path, table_names, *invalidators)
    payload = {
        "error": str(error),
        "status": "restore_failed" if restore_error else "rolled_back",
        "message": build_scoped_failure_message(backup_path, error, restore_error),
        "backup": backup_path,
    }
    if restore_error:
        payload["restore_error"] = str(restore_error)
    if extra_payload:
        payload.update(extra_payload)
    return jsonify(payload), status_code
