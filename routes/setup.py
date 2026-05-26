from flask import render_template, request, redirect, url_for, flash, jsonify, session

from modules.database import get_db_config_status, save_db_config_paths
from modules.database import sync_to_gdrive_now, sync_from_gdrive_now, get_last_sync_error
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
            cloud_provider = (request.form.get('cloud_provider') or 'onedrive').strip().lower()
            gdrive_sync_path = (request.form.get('gdrive_sync_path') or '').strip()
            onedrive_sync_path = (request.form.get('onedrive_sync_path') or '').strip()

            status = save_db_config_paths(
                db_path=None,
                gdrive_sync_path=gdrive_sync_path,
                onedrive_sync_path=onedrive_sync_path,
                cloud_provider=cloud_provider,
            )

            if status.get('is_ready'):
                session['setup_complete_once'] = True
                flash('Database paths saved and validated successfully.', 'success')
                return redirect(url_for('setup_requirements'))

            flash('Please fix the path issues below before continuing.', 'danger')
            return render_template('setup_storage.html', status=status)

        status = get_db_config_status()
        return render_template('setup_storage.html', status=status)

    @app.route('/setup/storage/push-backup', methods=['POST'])
    @require_login
    def setup_storage_push_backup():
        pushed = sync_to_gdrive_now()
        if pushed:
            flash('Backup push completed: local database copied to cloud backup.', 'success')
        else:
            detail = get_last_sync_error()
            if detail:
                flash(f'Backup push failed: {detail}', 'warning')
            else:
                flash('Backup push skipped or failed. Verify DB Backup path is configured and available.', 'warning')
        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/pull-backup', methods=['POST'])
    @require_login
    def setup_storage_pull_backup():
        pulled = sync_from_gdrive_now(force=True)
        if pulled:
            flash('Backup read completed: cloud backup copied to local database.', 'success')
        else:
            detail = get_last_sync_error()
            if detail:
                flash(f'Backup read failed: {detail}', 'warning')
            else:
                flash('Backup read skipped or failed. Verify cloud backup file exists and is reachable.', 'warning')
        return redirect(url_for('setup_storage'))

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
