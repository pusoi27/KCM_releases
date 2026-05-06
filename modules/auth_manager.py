"""
auth_manager.py - Single-user local install stub.

All multi-user login, registration, and password management logic has been removed.
This module exports only the feature-identifier constants that route decorators
import, plus a compatibility shim for get_current_user_id().
"""

# Feature identifier constants - retained so route decorators and routes can
# still import them without modification.
FEATURE_STUDENT_DATABASE = "student_database"
FEATURE_BOOKS = "books"
FEATURE_ASSISTANTS = "assistants"
FEATURE_KUMOCLASS = "kumoclass"
FEATURE_UTILITIES_PRINT = "utilities_print"
FEATURE_UTILITIES_EMAIL = "utilities_email"
FEATURE_INSTRUCTOR_PROFILE = "instructor_profile"
FEATURE_INSTRUCTOR_REPORTS = "instructor_reports"
FEATURE_INSTRUCTOR_SETTINGS = "instructor_settings"


def get_current_user_id():
    """Compatibility shim - single-user installs always return 1."""
    return 1


