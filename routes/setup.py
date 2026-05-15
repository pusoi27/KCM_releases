from flask import render_template, request, redirect, url_for, flash, jsonify, session

from modules.database import get_db_config_status, save_db_config_paths
from modules import instructor_profile_manager
from routes.auth import require_login


def _instructor_hours_status() -> dict:
    profile = instructor_profile_manager.get_instructor_profile()
    if not profile:
        return {
            'is_ready': False,
            'has_profile': False,
            'has_hours': False,
            'message': 'Create your instructor profile and add center class hours.',
        }

    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    has_hours = any(
        (profile.get(f'{day}_start') or '').strip() and (profile.get(f'{day}_end') or '').strip()
        for day in days
    )
    return {
        'is_ready': has_hours,
        'has_profile': True,
        'has_hours': has_hours,
        'message': (
            'Center class hours are configured.'
            if has_hours
            else 'Profile exists, but center class hours are still missing.'
        ),
    }


def register_setup_routes(app):
    @app.route('/setup', methods=['GET'])
    @require_login
    def setup_requirements():
        storage = get_db_config_status()
        profile_hours = _instructor_hours_status()

        ready = storage.get('is_ready') and profile_hours.get('is_ready')
        show_complete_banner = bool(session.get('setup_complete_once', False))
        auto_redirect_enabled = request.args.get('auto', '1').strip() != '0'

        if ready and not show_complete_banner:
            return redirect(url_for('dashboard'))
        if ready and show_complete_banner:
            session.pop('setup_complete_once', None)

        return render_template(
            'setup_requirements.html',
            storage_status=storage,
            profile_status=profile_hours,
            all_ready=ready,
            show_complete_banner=show_complete_banner,
            auto_redirect_enabled=auto_redirect_enabled,
            auto_redirect_seconds=3,
        )

    @app.route('/setup/storage', methods=['GET', 'POST'])
    @require_login
    def setup_storage():
        if request.method == 'POST':
            gdrive_sync_path = (request.form.get('gdrive_sync_path') or '').strip()
            sync_interval = (request.form.get('sync_interval_minutes') or '').strip()

            try:
                sync_interval_value = int(sync_interval) if sync_interval else None
            except ValueError:
                sync_interval_value = None

            status = save_db_config_paths(
                db_path=None,
                gdrive_sync_path=gdrive_sync_path,
                sync_interval_minutes=sync_interval_value,
            )

            if status.get('is_ready'):
                session['setup_complete_once'] = True
                flash('Database paths saved and validated successfully.', 'success')
                return redirect(url_for('setup_requirements'))

            flash('Please fix the path issues below before continuing.', 'danger')
            return render_template('setup_storage.html', status=status)

        status = get_db_config_status()
        return render_template('setup_storage.html', status=status)

    @app.route('/api/setup/status', methods=['GET'])
    @require_login
    def api_setup_status():
        storage = get_db_config_status()
        profile_hours = _instructor_hours_status()
        return jsonify({
            'storage': storage,
            'profile_hours': profile_hours,
            'all_ready': storage.get('is_ready') and profile_hours.get('is_ready'),
        })
