# routes/assistants.py
from flask import render_template, request, redirect, url_for, flash, send_file, jsonify
import io
import mimetypes
import os
from modules import assistant_manager, server_cache, db_backup_recovery, auth_manager
from routes.auth import require_login, require_admin, require_feature
from routes.operation_utils import flash_scoped_failure, invalidate_scoped_cache


def _save_assistant_photo(file_storage, assistant_id):
    """Validate and save an uploaded photo. Returns success status."""
    if not file_storage or not file_storage.filename:
        return False
    
    ext = file_storage.filename.rsplit('.', 1)[-1].lower() if '.' in file_storage.filename else ''
    allowed_exts = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
    if ext not in allowed_exts:
        return False
    
    try:
        icon_blob = file_storage.read()
        if not icon_blob:
            return False
        
        icon_mime = file_storage.mimetype or mimetypes.guess_type(file_storage.filename)[0] or 'image/png'
        assistant_manager.set_assistant_icon(assistant_id, icon_blob, icon_mime)
        return True
    except Exception:
        return False


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
                assistant_id = assistant_manager.add_assistant(
                    request.form["name"],
                    request.form.get("role", ""),
                    request.form.get("email", ""),
                    request.form.get("phone", "")
                )
                
                # Handle photo upload if provided
                photo_file = request.files.get('photo')
                if photo_file and photo_file.filename:
                    if _save_assistant_photo(photo_file, assistant_id):
                        flash("Staff member added successfully with photo.", "success")
                    else:
                        flash("Staff member added, but photo upload failed.", "warning")
                else:
                    flash("Staff member added successfully.", "success")
                
                invalidate_scoped_cache(lambda: _invalidate_assistants_cache())
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
                
                # Handle photo upload if provided
                photo_file = request.files.get('photo')
                if photo_file and photo_file.filename:
                    if _save_assistant_photo(photo_file, aid):
                        flash("Staff member updated with new photo.", "info")
                    else:
                        flash("Staff member updated, but photo upload failed.", "warning")
                else:
                    flash("Staff member updated.", "info")
                
                invalidate_scoped_cache(lambda: _invalidate_assistants_cache())
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

    @app.route("/staff/icon/<int:aid>", methods=["POST"])
    @require_login
    @require_feature(auth_manager.FEATURE_ASSISTANTS)
    def upload_staff_icon(aid):
        """Upload icon picture for a staff member."""
        asst = assistant_manager.get_assistant(aid)
        if not asst:
            return jsonify({'success': False, 'message': 'Staff member not found'}), 404
        
        icon_file = request.files.get('icon')
        if not icon_file or not icon_file.filename:
            return jsonify({'success': False, 'message': 'No file uploaded'}), 400
        
        # Validate file extension
        allowed_exts = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
        ext = icon_file.filename.rsplit('.', 1)[-1].lower() if '.' in icon_file.filename else ''
        if ext not in allowed_exts:
            return jsonify({'success': False, 'message': f'Invalid file type. Allowed: {", ".join(allowed_exts)}'}), 400
        
        try:
            icon_blob = icon_file.read()
            if not icon_blob:
                return jsonify({'success': False, 'message': 'File is empty'}), 400
            
            icon_mime = icon_file.mimetype or mimetypes.guess_type(icon_file.filename)[0] or 'image/png'
            assistant_manager.set_assistant_icon(aid, icon_blob, icon_mime)
            invalidate_scoped_cache(lambda: _invalidate_assistants_cache())
            return jsonify({'success': True, 'message': 'Icon uploaded successfully'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500

    @app.route("/staff/icon/<int:aid>")
    def serve_staff_icon(aid):
        """Serve staff member's icon picture."""
        icon_data = assistant_manager.get_assistant_icon(aid)
        if not icon_data:
            return "Icon not found", 404
        return send_file(
            io.BytesIO(icon_data['icon_blob']),
            mimetype=icon_data['icon_mime']
        )
