"""Access decorators for local single-user install."""

from functools import wraps

from flask import g, jsonify, redirect, render_template, request, session, url_for

from modules import license_manager
from modules import ls_license
from modules import user_identity_manager


def _license_denied_response():
    status = g.get('license_status') or license_manager.get_license_context()
    message = status.get('message', 'A valid license is required to use Stdytime.')
    if request.path.startswith('/api/'):
        return jsonify({'error': message, 'license_status': status.get('status', 'unlicensed')}), 403
    target = 'license_expired' if status.get('status') == 'expired' else 'license_page'
    return redirect(url_for(target))


def require_login(f):
    """Decorator: require a valid local license."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not (g.get('license_status') or {}).get('is_valid'):
            return _license_denied_response()
        return f(*args, **kwargs)
    return decorated_function


def require_admin(f):
    """Single-machine licensed installs always operate as the local owner."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not (g.get('license_status') or {}).get('is_valid'):
            return _license_denied_response()
        return f(*args, **kwargs)
    return decorated_function


def require_feature(feature):
    """Backward-compatible decorator; feature checks are disabled for single-user installs."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not (g.get('license_status') or {}).get('is_valid'):
                return _license_denied_response()
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def register_auth_routes(app):
    """Register local single-user email capture route."""

    @app.route('/login/email', methods=['GET', 'POST'])
    def email_login():
        if not (g.get('license_status') or {}).get('is_valid'):
            return _license_denied_response()

        next_url = (request.values.get('next') or '').strip()
        if not next_url.startswith('/'):
            next_url = '/'

        if request.method == 'POST':
            email = (request.form.get('email') or '').strip()
            if not user_identity_manager.is_valid_email(email):
                return render_template(
                    'auth/email_login.html',
                    error='Please enter a valid email address.',
                    email=email,
                    next_url=next_url,
                )

            # Validate the email against the LemonSqueezy license (if one is active).
            ls_error = ls_license.validate_email_matches_license(email)
            if ls_error:
                return render_template(
                    'auth/email_login.html',
                    error=ls_error,
                    email=email,
                    next_url=next_url,
                )

            session['user_email'] = email
            session.permanent = True
            user_identity_manager.save_email(email)
            user_identity_manager.sync_instructor_profile_email(email)
            return redirect(next_url)

        existing = user_identity_manager.resolve_active_email(session.get('user_email')) or ''
        return render_template(
            'auth/email_login.html',
            error=None,
            email=existing,
            next_url=next_url,
        )
