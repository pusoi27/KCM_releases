# routes/assistants.py
from flask import render_template, request, redirect, url_for, flash
from modules import assistant_manager, server_cache, db_backup_recovery, auth_manager
from routes.auth import require_login, require_admin, require_feature
from routes.operation_utils import flash_scoped_failure, invalidate_scoped_cache


def _assistants_profile_cache_key() -> str:
    return server_cache.ASSISTANTS_PROFILE_LIST_CACHE_KEY

def _assistants_duty_cache_key() -> str:
    return server_cache.ASSISTANTS_DUTY_LIST_CACHE_KEY

def _invalidate_assistants_cache():
    """Invalidate assistant profile + duty cache lanes."""
    server_cache.invalidate(_assistants_profile_cache_key())
    server_cache.invalidate(_assistants_duty_cache_key())


def register_assistant_routes(app):
    """Register assistant CRUD routes."""
    
    @app.route("/assistants")
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def assistants_list():
        assistants = server_cache.get_or_set(
            _assistants_profile_cache_key(),
            lambda: assistant_manager.get_all_assistants(),
            policy="assistant_profile",
        )
        return render_template(
            "assistants.html",
            assistants=assistants,
        )

    @app.route("/assistants/add", methods=["GET", "POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def assistants_add():
        if request.method == "POST":
            backup_path = db_backup_recovery.create_backup("assistants_add")
            try:
                assistant_manager.add_assistant(
                    request.form["name"],
                    request.form.get("role", ""),
                    request.form.get("email", ""),
                    request.form.get("phone", "")
                )
                invalidate_scoped_cache(lambda: _invalidate_assistants_cache())
                flash("Staff member added successfully.", "success")
            except Exception as e:
                flash_scoped_failure(
                    backup_path=backup_path,
                    table_names=("staff",),
                    error=e,
                    invalidators=(lambda: _invalidate_assistants_cache(),),
                )
            return redirect(url_for("assistants_list"))
        return render_template("assistant_form.html", action="Add", assistant=None)

    @app.route("/assistants/edit/<int:aid>", methods=["GET", "POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def assistants_edit(aid):
        asst = assistant_manager.get_assistant(aid)
        if not asst:
            return "Staff member not found", 404
        if request.method == "POST":
            backup_path = db_backup_recovery.create_backup("assistants_edit")
            try:
                assistant_manager.update_assistant(
                    aid,
                    request.form["name"],
                    request.form.get("role", ""),
                    request.form.get("email", ""),
                    request.form.get("phone", "")
                )
                invalidate_scoped_cache(lambda: _invalidate_assistants_cache())
                flash("Staff member updated.", "info")
            except Exception as e:
                flash_scoped_failure(
                    backup_path=backup_path,
                    table_names=("staff",),
                    error=e,
                    invalidators=(lambda: _invalidate_assistants_cache(),),
                )
            return redirect(url_for("assistants_list"))
        return render_template("assistant_form.html", action="Edit", assistant=asst)

    @app.route("/assistants/delete/<int:aid>", methods=["POST"])
    @require_admin
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def assistants_delete(aid):
        backup_path = db_backup_recovery.create_backup("assistants_delete")
        try:
            assistant_manager.delete_assistant(aid)
            invalidate_scoped_cache(lambda: _invalidate_assistants_cache())
            flash("Staff member deleted.", "warning")
        except Exception as e:
            flash_scoped_failure(
                backup_path=backup_path,
                table_names=("staff",),
                error=e,
                invalidators=(lambda: _invalidate_assistants_cache(),),
            )
        return redirect(url_for("assistants_list"))
