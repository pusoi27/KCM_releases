"""Routes for activating and managing the local Stdytime license."""

import logging

from flask import flash, jsonify, redirect, render_template, request, url_for

from modules import license_manager
from modules import ls_license
from modules.rate_limiter import limiter

logger = logging.getLogger(__name__)


def register_license_routes(app):
    """Register license activation and status routes."""

    @app.route('/license', methods=['GET'])
    def license_page():
        # Prefer LS context if an LS instance is stored, else fall back to local
        ls_ctx = ls_license.get_ls_license_context()
        context = ls_ctx if ls_ctx.get("has_license_key") else license_manager.get_license_context()
        return render_template('license.html', license_status=context)

    @app.route('/license/activate', methods=['POST'])
    @limiter.limit("10 per minute", error_message="Too many license activation attempts. Please wait a minute and try again.")
    def activate_license():
        license_key = request.form.get('license_key', '').strip()
        if not license_key:
            flash('Paste the license key you received after purchase.', 'danger')
            return redirect(url_for('license_page'))

        # Try LemonSqueezy activation first
        success, message, context = ls_license.activate_ls_license(license_key)
        if not success:
            # Fall back to local HMAC key for offline / dev use
            success, message, context = license_manager.activate_license(license_key)

        flash(message, 'success' if success else 'danger')
        if success:
            return redirect(url_for(context.get('default_home_endpoint', 'dashboard')))
        return redirect(url_for('license_page'))

    @app.route('/license/verify', methods=['POST'])
    @limiter.limit("5 per minute")
    def verify_license():
        """Re-check license validity with LemonSqueezy on demand."""
        valid, message = ls_license._verify_ls_license_uncached()
        return jsonify({"valid": valid, "message": message})

    @app.route('/license/remove', methods=['POST'])
    def remove_license():
        ls_license.deactivate_ls_license()
        license_manager.remove_license()
        flash('The license was removed from this machine.', 'warning')
        return redirect(url_for('license_page'))

    @app.route('/license/expired', methods=['GET'])
    def license_expired():
        ls_ctx = ls_license.get_ls_license_context()
        context = ls_ctx if ls_ctx.get("has_license_key") else license_manager.get_license_context()
        return render_template('license_expired.html', license_status=context)

    @app.route('/license/status', methods=['GET'])
    def license_status_api():
        ls_ctx = ls_license.get_ls_license_context()
        context = ls_ctx if ls_ctx.get("has_license_key") else license_manager.get_license_context()
        return jsonify(context)

    # -----------------------------------------------------------------------
    # LemonSqueezy Webhook
    # Configure in LS Dashboard → Webhooks → URL: https://yourhost/webhooks/lemonsqueezy
    # Events: license_key_created, license_key_activated, license_key_expired,
    #         license_key_disabled, subscription_updated, subscription_cancelled
    # -----------------------------------------------------------------------
    @app.route('/webhooks/lemonsqueezy', methods=['POST'])
    def lemonsqueezy_webhook():
        raw_body = request.get_data()
        signature = request.headers.get('X-Signature', '')

        if not ls_license.verify_webhook_signature(raw_body, signature):
            logger.warning("[webhook] Invalid LemonSqueezy signature from %s", request.remote_addr)
            return jsonify({"error": "Invalid signature"}), 401

        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            return jsonify({"error": "Invalid JSON body"}), 400

        result = ls_license.handle_ls_webhook_event(payload)
        logger.info("[webhook] %s", result)
        return jsonify({"received": True, "result": result}), 200

