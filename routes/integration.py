"""Integration API routes + Instructor UI for local external apps and plugin runtime."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from flask import flash, g, jsonify, redirect, render_template, request, url_for

from modules import integration_manager, plugin_sdk, student_manager
from modules.database import DB_PATH
from modules.email_manager import get_email_manager, render_branded_email_shell, resolve_center_name
from routes.auth import require_login


def _json_error(message: str, status: int = 400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def _format_utc_timestamp(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return raw


def _kctm_credentials_metadata() -> dict[str, str]:
    path = integration_manager.get_kctm_credentials_path()
    last_write = integration_manager.get_kctm_credentials_last_write()
    return {
        "path": path,
        "last_write": _format_utc_timestamp(last_write),
    }


def _license_status_payload() -> dict[str, Any]:
    status = integration_manager._license_context_snapshot()  # shared gate snapshot
    payload = {
        "ok": bool(status.get("ok")),
        "activated": bool(status.get("activated")),
        "license_valid": bool(status.get("license_valid")),
        "machine_match": bool(status.get("machine_match")),
        "license_tier": str(status.get("license_tier") or "pro"),
        "expires_at": str(status.get("expires_at") or ""),
        "issued_to": str(status.get("issued_to") or ""),
        "checked_at": str(status.get("checked_at") or ""),
        "reason": str(status.get("reason") or ""),
        "error": str(status.get("message") or ""),
        "is_activated": bool(status.get("activated")),
        "valid": bool(status.get("license_valid")),
        "hwid_match": bool(status.get("machine_match")),
    }
    return payload


def _fetch_audit_log(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent audit log rows as dicts."""
    integration_manager.ensure_integration_schema()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    id, key_id, key_prefix, action, method, path,
                    remote_addr, status_code, success, error, details_json, created_at
                FROM integration_audit_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _require_instructor_session_guard():
    if not integration_manager.is_local_request():
        return _json_error("Endpoint accepts local requests only.", 403)

    status = g.get("license_status") or {}
    if not status.get("is_valid"):
        return _json_error("A valid license is required.", 403)

    if not integration_manager.is_instructor_station():
        return _json_error("Endpoint is available only on Instructor Station.", 403)

    return None


def _student_payload_from_database_row(row: dict[str, Any]) -> dict[str, Any]:
    schedule = row.get("schedule") or []
    return {
        "id": int(row.get("id") or 0),
        "name": str(row.get("name") or ""),
        "student_identifier": str(row.get("student_identifier") or ""),
        "email": str(row.get("email") or ""),
        "phone": str(row.get("phone") or ""),
        "guardian": str(row.get("guardian") or ""),
        "subjects": list(row.get("subjects") or []),
        "classification": str(row.get("classification") or ""),
        "active": bool(row.get("active")),
        "checkout_notify_enabled": bool(row.get("checkout_notify_enabled", True)),
        "schedule": schedule,
        "photo_url": str(row.get("photo_url") or ""),
    }


def register_integration_routes(app):
    integration_manager.ensure_integration_schema()
    plugin_sdk.load_plugins(force_reload=False)

    @app.route("/integration/v1/students", methods=["GET"])
    @integration_manager.require_integration_auth([integration_manager.INTEGRATION_SCOPE_STUDENTS_READ])
    def integration_students_list():
        include_inactive = str(request.args.get("include_inactive") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
            "on",
        }

        active_rows = student_manager.get_student_database_rows(active=1)
        rows = list(active_rows)
        if include_inactive:
            rows.extend(student_manager.get_student_database_rows(active=0))

        payload = [_student_payload_from_database_row(row) for row in rows]

        integration_manager.log_audit(
            action="integration_students_list",
            status_code=200,
            success=True,
            key_id=(g.get("integration_client") or {}).get("id"),
            key_prefix=(g.get("integration_client") or {}).get("key_prefix", ""),
            details={"count": len(payload), "include_inactive": include_inactive},
        )
        return jsonify({"ok": True, "students": payload, "count": len(payload)}), 200

    @app.route("/integration/v1/license/status", methods=["GET"])
    @integration_manager.require_integration_auth([integration_manager.INTEGRATION_SCOPE_LICENSE_READ])
    def integration_license_status():
        payload = _license_status_payload()
        integration_manager.log_audit(
            action="integration_license_status",
            status_code=200,
            success=True,
            key_id=(g.get("integration_client") or {}).get("id"),
            key_prefix=(g.get("integration_client") or {}).get("key_prefix", ""),
            details={
                "activated": payload.get("activated"),
                "license_valid": payload.get("license_valid"),
                "machine_match": payload.get("machine_match"),
            },
        )
        return jsonify(payload), 200

    @app.route("/integration/v1/emails/send", methods=["POST"])
    @integration_manager.require_integration_auth([integration_manager.INTEGRATION_SCOPE_EMAILS_SEND])
    def integration_email_send():
        data = request.get_json(silent=True) or {}

        recipient_email = str(data.get("recipient_email") or "").strip()
        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
        html_body = data.get("html_body")

        if not recipient_email or "@" not in recipient_email:
            integration_manager.log_audit(
                action="integration_email_send",
                status_code=400,
                success=False,
                key_id=(g.get("integration_client") or {}).get("id"),
                key_prefix=(g.get("integration_client") or {}).get("key_prefix", ""),
                error="invalid_recipient",
            )
            return _json_error("Valid recipient_email is required.", 400)

        if not subject:
            return _json_error("subject is required.", 400)
        if not body:
            return _json_error("body is required.", 400)

        use_brand_shell = bool(data.get("use_brand_shell", True))
        center_name = resolve_center_name()

        email_payload = {
            "recipient_email": recipient_email,
            "subject": subject,
            "body": body,
            "html_body": html_body if isinstance(html_body, str) else "",
            "center_name": center_name,
            "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        }

        email_payload = plugin_sdk.invoke_hook("before_email_send", email_payload)

        if use_brand_shell and not str(email_payload.get("html_body") or "").strip():
            body_html = "".join(
                f"<p>{line}</p>" if str(line).strip() else "<p>&nbsp;</p>"
                for line in str(email_payload.get("body") or "").splitlines()
            )
            email_payload["html_body"] = render_branded_email_shell(
                title=str(email_payload.get("subject") or subject),
                center_name=str(email_payload.get("center_name") or center_name),
                body_html=body_html,
            )

        manager = get_email_manager()
        result = manager.send_email(
            recipient_email=str(email_payload.get("recipient_email") or recipient_email),
            subject=str(email_payload.get("subject") or subject),
            body=str(email_payload.get("body") or body),
            html_body=str(email_payload.get("html_body") or "") or None,
        )

        plugin_sdk.invoke_hook(
            "after_email_send",
            {
                "request": email_payload,
                "result": result,
            },
        )

        ok = bool(result.get("success"))
        status_code = 200 if ok else 502
        message_seed = "|".join(
            [
                recipient_email,
                subject,
                body,
                str(email_payload.get("html_body") or ""),
            ]
        )
        message_id = f"msg_{hashlib.sha256(message_seed.encode('utf-8')).hexdigest()[:12]}"
        integration_manager.log_audit(
            action="integration_email_send",
            status_code=status_code,
            success=ok,
            key_id=(g.get("integration_client") or {}).get("id"),
            key_prefix=(g.get("integration_client") or {}).get("key_prefix", ""),
            error=str(result.get("error") or "") if not ok else "",
            details={"recipient": recipient_email},
        )
        response = {
            "ok": ok,
            "message_id": message_id,
            "queue_id": message_id,
            "provider": "smtp",
            "queued": False,
        }
        if ok:
            response["message"] = str(result.get("message") or "Email sent successfully.")
        else:
            response["error"] = str(result.get("error") or "Email send failed.")
        return jsonify(response), status_code

    @app.route("/integration/v1/plugins", methods=["GET"])
    @integration_manager.require_integration_auth([integration_manager.INTEGRATION_SCOPE_PLUGINS_READ])
    def integration_plugins_list():
        plugins = plugin_sdk.get_loaded_plugins()
        integration_manager.log_audit(
            action="integration_plugins_list",
            status_code=200,
            success=True,
            key_id=(g.get("integration_client") or {}).get("id"),
            key_prefix=(g.get("integration_client") or {}).get("key_prefix", ""),
            details={"plugins": len(plugins)},
        )
        return jsonify(plugins), 200

    @app.route("/integration/v1/keys", methods=["GET"])
    @require_login
    def integration_keys_list():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error
        return jsonify(integration_manager.list_api_keys()), 200

    @app.route("/integration/v1/keys", methods=["POST"])
    @require_login
    def integration_keys_create():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip() or "External Local App"
        scopes = data.get("scopes") if isinstance(data.get("scopes"), list) else list(integration_manager.DEFAULT_SCOPES)
        rate_limit = int(data.get("rate_limit_per_minute") or 120)

        created = integration_manager.create_api_key(
            name=name,
            scopes=scopes,
            rate_limit_per_minute=rate_limit,
            bound_hwid=integration_manager.local_hwid(),
        )
        shared = integration_manager.share_api_key_with_kctm(
            created,
            stdytime_base_url=request.host_url.rstrip("/"),
        )
        integration_manager.log_audit(
            action="integration_key_create",
            status_code=201,
            success=True,
            key_id=created.get("id"),
            key_prefix=created.get("key_prefix", ""),
            details={"name": name, "scopes": scopes, "kctm_shared": bool(shared.get("ok"))},
        )
        return jsonify({**created, "kctm_shared": bool(shared.get("ok")), "kctm_shared_path": shared.get("path", "")}), 201

    @app.route("/integration/v1/keys/<int:key_id>", methods=["DELETE"])
    @require_login
    def integration_keys_revoke(key_id: int):
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        ok = integration_manager.revoke_api_key(key_id)
        if not ok:
            return _json_error("Key not found.", 404)

        integration_manager.log_audit(
            action="integration_key_revoke",
            status_code=200,
            success=True,
            key_id=key_id,
            details={},
        )
        return jsonify({"success": True, "key_id": key_id}), 200

    @app.route("/integration/v1/hwid", methods=["GET"])
    @require_login
    def integration_hwid_view():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error
        return jsonify({"hwid": integration_manager.local_hwid()}), 200

    # ------------------------------------------------------------------
    #  Phase C.1 — Instructor UI pages (session-auth, no API key needed)
    # ------------------------------------------------------------------

    @app.route("/integration/manage", methods=["GET"])
    @require_login
    def integration_ui():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        api_keys = integration_manager.list_api_keys()
        plugins = plugin_sdk.get_loaded_plugins()
        hwid = integration_manager.local_hwid()
        audit_log = _fetch_audit_log(limit=50)
        kctm_credentials = _kctm_credentials_metadata()

        new_key = request.args.get("_new_key", "")

        return render_template(
            "integration_keys.html",
            api_keys=api_keys,
            plugins=plugins,
            hwid=hwid,
            audit_log=audit_log,
            new_key=new_key,
            kctm_credentials_path=kctm_credentials["path"],
            kctm_credentials_last_write=kctm_credentials["last_write"],
        )

    @app.route("/integration/manage/keys/create", methods=["POST"])
    @require_login
    def integration_ui_create_key():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        name = str(request.form.get("name") or "").strip() or "External Local App"
        scopes = [s.strip() for s in request.form.getlist("scopes") if s.strip()]
        rate_limit = max(10, min(600, int(request.form.get("rate_limit_per_minute") or 120)))

        if not scopes:
            scopes = list(integration_manager.DEFAULT_SCOPES)

        created = integration_manager.create_api_key(
            name=name,
            scopes=scopes,
            rate_limit_per_minute=rate_limit,
            bound_hwid=integration_manager.local_hwid(),
        )
        shared = integration_manager.share_api_key_with_kctm(
            created,
            stdytime_base_url=request.host_url.rstrip("/"),
        )
        integration_manager.log_audit(
            action="integration_key_create_ui",
            status_code=201,
            success=True,
            key_id=created.get("id"),
            key_prefix=created.get("key_prefix", ""),
            details={"name": name, "scopes": scopes, "kctm_shared": bool(shared.get("ok"))},
        )
        if shared.get("ok"):
            flash("API key created and shared with KCTM local credentials file.", "success")
        else:
            flash("API key created. Copy the key below — it won't be shown again.", "success")
        return redirect(url_for("integration_ui", _new_key=created["api_key"]))

    @app.route("/integration/manage/kctm/credentials/regenerate", methods=["POST"])
    @require_login
    def integration_kctm_credentials_regenerate():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        refreshed = integration_manager.refresh_kctm_credentials_file(
            stdytime_base_url=request.host_url.rstrip("/"),
        )
        if refreshed.get("ok"):
            integration_manager.log_audit(
                action="integration_kctm_credentials_regenerate",
                status_code=200,
                success=True,
                key_prefix="",
                details={"path": refreshed.get("path", "")},
            )
            flash(f"KCTM credentials file refreshed at {refreshed.get('path', '')}.", "success")
        else:
            flash(f"Could not refresh KCTM credentials file: {refreshed.get('error', 'unknown error')}", "warning")
        return redirect(url_for("integration_ui"))

    @app.route("/integration/manage/keys/<int:key_id>/revoke", methods=["POST"])
    @require_login
    def integration_ui_revoke_key(key_id: int):
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        ok = integration_manager.revoke_api_key(key_id)
        if ok:
            integration_manager.log_audit(
                action="integration_key_revoke_ui",
                status_code=200,
                success=True,
                key_id=key_id,
                details={},
            )
            flash("API key revoked.", "success")
        else:
            flash("Key not found.", "warning")
        return redirect(url_for("integration_ui"))

    @app.route("/integration/plugins/readme", methods=["GET"])
    @require_login
    def integration_plugins_readme():
        guard_error = _require_instructor_session_guard()
        if guard_error:
            return guard_error

        import os
        readme_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugins",
            "README.md",
        )
        readme_content = ""
        if os.path.exists(readme_path):
            try:
                with open(readme_path, encoding="utf-8") as fh:
                    readme_content = fh.read()
            except Exception:
                readme_content = "README not available."
        else:
            readme_content = "README not found."

        return render_template(
            "integration_plugins_readme.html",
            readme_content=readme_content,
            plugins_root=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins"
            ),
        )
