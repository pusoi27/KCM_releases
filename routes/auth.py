"""Access decorators for local single-user install."""

from functools import wraps

from flask import g, jsonify, redirect, request, url_for

from modules import license_manager


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
    """No login routes for single-user local installs."""
    pass
