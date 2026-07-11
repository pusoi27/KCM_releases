"""
modules/rate_limiter.py - Shared Flask-Limiter instance.

Initialized here to avoid circular imports between app.py and route modules.
Call limiter.init_app(app) in app.py after the Flask app is created.
"""

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri="memory://",
    default_limits=[],          # No global default; limits applied per-route
)
