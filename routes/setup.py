from flask import render_template, request, redirect, url_for, flash, jsonify, session
import secrets
import requests
import socket
import threading
import time

from modules.database import get_db_config_status, save_db_config_paths, get_station_runtime_config
from modules.database import sync_to_gdrive_now, sync_from_gdrive_now, get_last_sync_error
from modules.database import get_database_health_report, backup_database_now, restore_database_now
from modules.database import run_manual_wal_checkpoint
from modules import instructor_profile_manager
from routes.auth import require_login


_PAIR_CODE_TTL_SECONDS = 180
_PAIR_CODE_LOCK = threading.Lock()
_PAIR_CODE_STATE = {
    'code': '',
    'expires_at': 0.0,
}


def _normalize_base_url(raw_url: str) -> str:
    value = str(raw_url or '').strip()
    if not value:
        return ''
    value = value.replace(' ', '')
    if '://' not in value:
        value = f"http://{value}"
    value = value.rstrip('/')
    # Common typo recovery: 192.168.1.5.:5000 -> 192.168.1.5:5000
    value = value.replace('.:', ':')
    return value


def _detect_lan_ip() -> str:
    """Best-effort local LAN IP detection (no external traffic sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            return str(ip or '').strip()
    except Exception:
        return ''


def _resolve_advertised_instructor_base_url(runtime: dict, host_url: str) -> str:
    """Resolve the URL scanner should use to reach Instructor API.

    Priority:
    1) explicit configured instructor_api_base_url (editable by user)
    2) detected LAN IP + request port
    3) request host URL fallback
    """
    configured = _normalize_base_url((runtime or {}).get('instructor_api_base_url') or '')
    if configured:
        return configured

    lan_ip = _detect_lan_ip()
    if lan_ip:
        base = str(host_url or '').strip().rstrip('/')
        # host_url is usually like http://127.0.0.1:5000/
        try:
            host_port = base.split('://', 1)[1]
            if ':' in host_port:
                port = host_port.split(':', 1)[1]
                return f"http://{lan_ip}:{port}"
        except Exception:
            pass
        return f"http://{lan_ip}:5000"

    return str(host_url or '').rstrip('/')


def _issue_pair_code() -> tuple[str, int]:
    code = f"{secrets.randbelow(10000):04d}"
    now = time.time()
    expires = now + _PAIR_CODE_TTL_SECONDS
    with _PAIR_CODE_LOCK:
        _PAIR_CODE_STATE['code'] = code
        _PAIR_CODE_STATE['expires_at'] = expires
    return code, int(_PAIR_CODE_TTL_SECONDS)


def _pair_code_is_valid(code: str) -> bool:
    token = str(code or '').strip()
    now = time.time()
    with _PAIR_CODE_LOCK:
        expected = str(_PAIR_CODE_STATE.get('code') or '').strip()
        expires_at = float(_PAIR_CODE_STATE.get('expires_at') or 0.0)
    return bool(token and expected and token == expected and now <= expires_at)


def _current_pair_code_display() -> tuple[str, int]:
    """Return currently valid pair code and remaining seconds."""
    now = time.time()
    with _PAIR_CODE_LOCK:
        code = str(_PAIR_CODE_STATE.get('code') or '').strip()
        expires_at = float(_PAIR_CODE_STATE.get('expires_at') or 0.0)
    if not code or expires_at <= now:
        return '', 0
    return code, int(max(0, expires_at - now))


def _save_runtime_from_cfg(cfg: dict, *, station_mode: str, instructor_api_base_url: str = '', station_pairing_token: str = '') -> dict:
    return save_db_config_paths(
        db_path=None,
        gdrive_sync_path='',
        onedrive_sync_path=(cfg.get('onedrive_sync_path') or ''),
        cloud_provider=(cfg.get('cloud_provider') or 'onedrive'),
        station_mode=station_mode,
        backup_mode='instructor_snapshots_only',
        instructor_api_base_url=instructor_api_base_url,
        station_pairing_token=station_pairing_token,
        snapshot_interval_minutes=int(cfg.get('snapshot_interval_minutes') or 15),
    )


def _attempt_pairing_bootstrap(instructor_base_url: str, timeout_seconds: int = 8) -> tuple[bool, str, dict]:
    base_url = _normalize_base_url(instructor_base_url)
    if not base_url:
        return False, 'Instructor API URL is empty.', {}

    bootstrap_url = f"{base_url}/api/station/pairing/bootstrap"
    try:
        resp = requests.get(bootstrap_url, headers={"Accept": "application/json"}, timeout=timeout_seconds)
    except requests.RequestException as exc:
        return False, f"Cannot reach instructor bootstrap endpoint: {exc}", {}

    payload = resp.json() if resp.headers.get('content-type', '').lower().startswith('application/json') else {}
    if not resp.ok or not payload.get('ok'):
        reason = payload.get('error') or f'HTTP {resp.status_code}'
        return False, f'Instructor bootstrap failed: {reason}', payload if isinstance(payload, dict) else {}

    token = str(payload.get('station_pairing_token') or '').strip()
    resolved_base = _normalize_base_url(payload.get('instructor_api_base_url') or base_url)
    if not token:
        return False, 'Instructor bootstrap response did not include pairing token.', payload if isinstance(payload, dict) else {}

    return True, 'ok', {
        'station_pairing_token': token,
        'instructor_api_base_url': resolved_base,
    }


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
        from flask import g
        storage = get_db_config_status()
        profile_hours = _instructor_hours_status()

        _ls = g.get('license_status') or {}
        is_scanner_station = (
            int(_ls.get('activation_limit') or 0) >= 2
            and str(_ls.get('station_role') or '').strip().lower() == 'checkin'
        )

        # Scanner stations in a two-machine setup skip the instructor profile requirement.
        ready = storage.get('is_ready') and (profile_hours.get('is_ready') or is_scanner_station)
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
            is_scanner_station=is_scanner_station,
        )

    @app.route('/setup/storage', methods=['GET', 'POST'])
    @require_login
    def setup_storage():
        if request.method == 'POST':
            cloud_provider = (request.form.get('cloud_provider') or 'onedrive').strip().lower()
            gdrive_sync_path = (request.form.get('gdrive_sync_path') or '').strip()
            onedrive_sync_path = (request.form.get('onedrive_sync_path') or '').strip()
            station_mode = (request.form.get('station_mode') or 'instructor_server').strip().lower()
            backup_mode = 'instructor_snapshots_only'
            instructor_api_base_url = _normalize_base_url(request.form.get('instructor_api_base_url') or '')
            station_pairing_token = (request.form.get('station_pairing_token') or '').strip()
            snapshot_interval_minutes_raw = (request.form.get('snapshot_interval_minutes') or '15').strip()
            try:
                snapshot_interval_minutes = max(5, int(snapshot_interval_minutes_raw or '15'))
            except ValueError:
                snapshot_interval_minutes = 15

            # Minimize scanner setup friction: auto-fetch token from instructor when URL is provided.
            if station_mode == 'scanner_api_client' and instructor_api_base_url and not station_pairing_token:
                ok, msg, bootstrap = _attempt_pairing_bootstrap(instructor_api_base_url)
                if ok:
                    station_pairing_token = str(bootstrap.get('station_pairing_token') or '').strip()
                    instructor_api_base_url = _normalize_base_url(
                        bootstrap.get('instructor_api_base_url') or instructor_api_base_url
                    )
                    flash('Auto-connect: pairing token fetched from Instructor Station.', 'success')
                else:
                    flash(f'Auto-connect did not complete: {msg}', 'warning')

            # Minimize instructor setup friction: always ensure a token exists.
            if station_mode == 'instructor_server' and not station_pairing_token:
                station_pairing_token = secrets.token_urlsafe(24)
                flash('Instructor token auto-generated for scanner pairing.', 'success')

            # For instructor mode, auto-fill a practical LAN URL when empty.
            if station_mode == 'instructor_server' and not instructor_api_base_url:
                instructor_api_base_url = _resolve_advertised_instructor_base_url(
                    {'instructor_api_base_url': ''},
                    request.host_url,
                )

            status = save_db_config_paths(
                db_path=None,
                gdrive_sync_path=gdrive_sync_path,
                onedrive_sync_path=onedrive_sync_path,
                cloud_provider=cloud_provider,
                station_mode=station_mode,
                backup_mode=backup_mode,
                instructor_api_base_url=instructor_api_base_url,
                station_pairing_token=station_pairing_token,
                snapshot_interval_minutes=snapshot_interval_minutes,
            )

            if status.get('is_ready'):
                session['setup_complete_once'] = True
                flash('Database paths saved and validated successfully.', 'success')
                return redirect(url_for('setup_requirements'))

            flash('Please fix the path issues below before continuing.', 'danger')
            runtime = get_station_runtime_config()
            display_pair_code, display_pair_code_ttl = _current_pair_code_display()
            pairing_endpoint = '/api/station/pairing/ping'
            suggested_pairing_url = (
                f"{request.host_url.rstrip('/')}{pairing_endpoint}"
                if runtime.get('station_mode') == 'instructor_server'
                else f"{(runtime.get('instructor_api_base_url') or '').rstrip('/')}{pairing_endpoint}"
            )
            return render_template(
                'setup_storage.html',
                status=status,
                runtime=runtime,
                suggested_pairing_url=suggested_pairing_url,
                display_pair_code=display_pair_code,
                display_pair_code_ttl=display_pair_code_ttl,
            )

        status = get_db_config_status()
        runtime = get_station_runtime_config()
        if str(runtime.get('station_mode') or '').strip().lower() == 'instructor_server' and not str(runtime.get('instructor_api_base_url') or '').strip():
            runtime['instructor_api_base_url'] = _resolve_advertised_instructor_base_url(runtime, request.host_url)

        display_pair_code, display_pair_code_ttl = _current_pair_code_display()
        pairing_endpoint = '/api/station/pairing/ping'
        suggested_pairing_url = (
            f"{request.host_url.rstrip('/')}{pairing_endpoint}"
            if runtime.get('station_mode') == 'instructor_server'
            else f"{(runtime.get('instructor_api_base_url') or '').rstrip('/')}{pairing_endpoint}"
        )
        return render_template(
            'setup_storage.html',
            status=status,
            runtime=runtime,
            suggested_pairing_url=suggested_pairing_url,
            display_pair_code=display_pair_code,
            display_pair_code_ttl=display_pair_code_ttl,
        )

    @app.route('/setup/storage/show-pair-code', methods=['POST'])
    @require_login
    def setup_storage_show_pair_code():
        runtime = get_station_runtime_config()
        if str(runtime.get('station_mode') or '').strip().lower() != 'instructor_server':
            flash('4-digit pairing code can only be generated on Instructor Station mode.', 'warning')
            return redirect(url_for('setup_storage'))

        # Ensure token exists for eventual scanner API auth.
        status = get_db_config_status()
        cfg = (status or {}).get('config', {}) if isinstance(status, dict) else {}
        token = str(runtime.get('station_pairing_token') or '').strip()
        if not token:
            token = secrets.token_urlsafe(24)

        advertised = _resolve_advertised_instructor_base_url(runtime, request.host_url)
        _save_runtime_from_cfg(
            cfg,
            station_mode='instructor_server',
            instructor_api_base_url=advertised,
            station_pairing_token=token,
        )

        code, ttl = _issue_pair_code()
        session['display_pair_code'] = code
        session['display_pair_code_ttl'] = ttl
        flash(f'Pairing code ready: {code} (expires in {ttl} seconds).', 'success')
        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/pair-by-code', methods=['POST'])
    @require_login
    def setup_storage_pair_by_code():
        status = get_db_config_status()
        cfg = (status or {}).get('config', {}) if isinstance(status, dict) else {}

        base_url = _normalize_base_url(request.form.get('instructor_api_base_url') or cfg.get('instructor_api_base_url') or '')
        code = str(request.form.get('pair_code') or '').strip()

        if not base_url:
            flash('Pair by code failed: Instructor API URL is required.', 'warning')
            return redirect(url_for('setup_storage'))
        if not code or len(code) != 4 or not code.isdigit():
            flash('Pair by code failed: enter the 4-digit code shown on Instructor.', 'warning')
            return redirect(url_for('setup_storage'))

        exchange_url = f"{base_url}/api/station/pairing/code/exchange"
        try:
            resp = requests.post(
                exchange_url,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"pair_code": code},
                timeout=8,
            )
            payload = resp.json() if resp.headers.get('content-type', '').lower().startswith('application/json') else {}
        except requests.RequestException as exc:
            flash(f'Pair by code failed: cannot reach instructor ({exc}).', 'warning')
            return redirect(url_for('setup_storage'))

        if not resp.ok or not payload.get('ok'):
            reason = payload.get('error') or f'HTTP {resp.status_code}'
            flash(f'Pair by code failed: {reason}', 'warning')
            return redirect(url_for('setup_storage'))

        token = str(payload.get('station_pairing_token') or '').strip()
        resolved_base = _normalize_base_url(payload.get('instructor_api_base_url') or base_url)
        if not token:
            flash('Pair by code failed: instructor did not return pairing token.', 'warning')
            return redirect(url_for('setup_storage'))

        save_db_config_paths(
            db_path=None,
            gdrive_sync_path='',
            onedrive_sync_path=(cfg.get('onedrive_sync_path') or ''),
            cloud_provider=(cfg.get('cloud_provider') or 'onedrive'),
            station_mode='scanner_api_client',
            backup_mode='instructor_snapshots_only',
            instructor_api_base_url=resolved_base,
            station_pairing_token=token,
            snapshot_interval_minutes=int(cfg.get('snapshot_interval_minutes') or 15),
        )

        flash('Pairing successful: Scanner linked to Instructor using 4-digit code.', 'success')
        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/auto-connect', methods=['POST'])
    @require_login
    def setup_storage_auto_connect():
        status = get_db_config_status()
        cfg = (status or {}).get('config', {}) if isinstance(status, dict) else {}

        base_url = _normalize_base_url(request.form.get('instructor_api_base_url') or cfg.get('instructor_api_base_url') or '')
        if not base_url:
            flash('Auto-connect failed: Instructor API URL is required.', 'warning')
            return redirect(url_for('setup_storage'))

        ok, msg, bootstrap = _attempt_pairing_bootstrap(base_url)
        if not ok:
            flash(f'Auto-connect failed: {msg}', 'warning')
            return redirect(url_for('setup_storage'))

        token = str(bootstrap.get('station_pairing_token') or '').strip()
        resolved_base = _normalize_base_url(bootstrap.get('instructor_api_base_url') or base_url)

        save_db_config_paths(
            db_path=None,
            gdrive_sync_path='',
            onedrive_sync_path=(cfg.get('onedrive_sync_path') or ''),
            cloud_provider=(cfg.get('cloud_provider') or 'onedrive'),
            station_mode='scanner_api_client',
            backup_mode='instructor_snapshots_only',
            instructor_api_base_url=resolved_base,
            station_pairing_token=token,
            snapshot_interval_minutes=int(cfg.get('snapshot_interval_minutes') or 15),
        )

        ping_url = f"{resolved_base}/api/station/pairing/ping"
        try:
            resp = requests.get(
                ping_url,
                headers={"X-Stdytime-Pairing-Token": token, "Accept": "application/json"},
                timeout=8,
            )
            payload = resp.json() if resp.headers.get('content-type', '').lower().startswith('application/json') else {}
            if resp.ok and payload.get('ok'):
                flash('Auto-connect successful: Scanner linked to Instructor Station.', 'success')
            else:
                reason = payload.get('error') or f'HTTP {resp.status_code}'
                flash(f'Auto-connect saved settings, but pair test failed: {reason}', 'warning')
        except requests.RequestException as exc:
            flash(f'Auto-connect saved settings, but pair test failed: {exc}', 'warning')

        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/generate-pairing-token', methods=['POST'])
    @require_login
    def setup_storage_generate_pairing_token():
        status = get_db_config_status()
        cfg = (status or {}).get('config', {}) if isinstance(status, dict) else {}
        generated_token = secrets.token_urlsafe(24)

        _save_runtime_from_cfg(
            cfg,
            station_mode=(cfg.get('station_mode') or 'instructor_server'),
            instructor_api_base_url=_normalize_base_url(cfg.get('instructor_api_base_url') or ''),
            station_pairing_token=generated_token,
        )

        flash('New pairing token generated and saved.', 'success')
        return redirect(url_for('setup_storage'))

    @app.route('/api/station/pairing/bootstrap', methods=['GET'])
    def api_station_pairing_bootstrap():
        """Expose scanner bootstrap info from Instructor station for low-input setup."""
        status = get_db_config_status()
        cfg = (status or {}).get('config', {}) if isinstance(status, dict) else {}
        runtime = get_station_runtime_config()

        if str(runtime.get('station_mode') or '').strip().lower() != 'instructor_server':
            return jsonify({'ok': False, 'error': 'This machine is not configured as Instructor API Server.'}), 409

        token = str(runtime.get('station_pairing_token') or '').strip()
        if not token:
            token = secrets.token_urlsafe(24)
            _save_runtime_from_cfg(
                cfg,
                station_mode='instructor_server',
                instructor_api_base_url='',
                station_pairing_token=token,
            )

        advertised = _resolve_advertised_instructor_base_url(runtime, request.host_url)

        return jsonify({
            'ok': True,
            'station_mode': 'instructor_server',
            'backup_mode': 'instructor_snapshots_only',
            'instructor_api_base_url': advertised,
            'station_pairing_token': token,
        }), 200

    @app.route('/api/station/pairing/code/exchange', methods=['POST'])
    def api_station_pairing_code_exchange():
        runtime = get_station_runtime_config()
        if str(runtime.get('station_mode') or '').strip().lower() != 'instructor_server':
            return jsonify({'ok': False, 'error': 'This machine is not configured as Instructor API Server.'}), 409

        payload = request.get_json(silent=True) or {}
        incoming_code = str(payload.get('pair_code') or '').strip()
        if not _pair_code_is_valid(incoming_code):
            return jsonify({'ok': False, 'error': 'Invalid or expired pairing code.'}), 401

        status = get_db_config_status()
        cfg = (status or {}).get('config', {}) if isinstance(status, dict) else {}

        token = str(runtime.get('station_pairing_token') or '').strip()
        if not token:
            token = secrets.token_urlsafe(24)
            _save_runtime_from_cfg(
                cfg,
                station_mode='instructor_server',
                instructor_api_base_url=_resolve_advertised_instructor_base_url(runtime, request.host_url),
                station_pairing_token=token,
            )

        advertised = _resolve_advertised_instructor_base_url(runtime, request.host_url)

        return jsonify({
            'ok': True,
            'station_mode': 'instructor_server',
            'backup_mode': 'instructor_snapshots_only',
            'instructor_api_base_url': advertised,
            'station_pairing_token': token,
        }), 200

    try:
        from app import csrf
        csrf.exempt(api_station_pairing_code_exchange)
    except Exception:
        pass

    @app.route('/setup/storage/test-pairing', methods=['POST'])
    @require_login
    def setup_storage_test_pairing():
        runtime = get_station_runtime_config()
        base_url = _normalize_base_url(runtime.get('instructor_api_base_url') or '')
        token = str(runtime.get('station_pairing_token') or '').strip()

        if not base_url:
            flash('Pairing test failed: Instructor API URL is empty.', 'warning')
            return redirect(url_for('setup_storage'))
        if not token:
            flash('Pairing test failed: pairing token is empty.', 'warning')
            return redirect(url_for('setup_storage'))

        ping_url = f"{base_url}/api/station/pairing/ping"
        try:
            resp = requests.get(
                ping_url,
                headers={"X-Stdytime-Pairing-Token": token, "Accept": "application/json"},
                timeout=8,
            )
            payload = resp.json() if resp.headers.get('content-type', '').lower().startswith('application/json') else {}
            if resp.ok and payload.get('ok'):
                flash('Pair connection successful: scanner can reach instructor API.', 'success')
            else:
                reason = payload.get('error') or f'HTTP {resp.status_code}'
                flash(f'Pair connection failed: {reason}', 'warning')
        except requests.RequestException as exc:
            flash(f'Pair connection failed: {exc}', 'warning')

        return redirect(url_for('setup_storage'))

    @app.route('/api/station/pairing/ping', methods=['GET'])
    def api_station_pairing_ping():
        runtime = get_station_runtime_config()
        expected = str(runtime.get('station_pairing_token') or '').strip()
        received = str(request.headers.get('X-Stdytime-Pairing-Token') or '').strip()
        if not expected:
            return jsonify({'ok': False, 'error': 'Pairing token is not configured on instructor station.'}), 503
        if expected != received:
            return jsonify({'ok': False, 'error': 'Invalid pairing token.'}), 401
        return jsonify({'ok': True, 'station_mode': runtime.get('station_mode'), 'backup_mode': runtime.get('backup_mode')}), 200

    @app.route('/api/station/connection-status', methods=['GET'])
    @require_login
    def api_station_connection_status():
        runtime = get_station_runtime_config()
        station_mode = str(runtime.get('station_mode') or '').strip().lower()

        if station_mode != 'scanner_api_client':
            return jsonify({
                'visible': False,
                'connected': True,
                'tone': 'secondary',
                'label': 'Station Link: n/a',
                'tooltip': 'Connection badge is only used on Scanner API Client mode.',
            }), 200

        base_url = str(runtime.get('instructor_api_base_url') or '').strip().rstrip('/')
        token = str(runtime.get('station_pairing_token') or '').strip()
        if not base_url:
            return jsonify({
                'visible': True,
                'connected': False,
                'tone': 'warning',
                'label': 'Station Link: setup',
                'tooltip': 'Instructor API URL is not configured.',
            }), 200
        if not token:
            return jsonify({
                'visible': True,
                'connected': False,
                'tone': 'warning',
                'label': 'Station Link: token',
                'tooltip': 'Pairing token is not configured.',
            }), 200

        ping_url = f"{base_url}/api/station/pairing/ping"
        try:
            resp = requests.get(
                ping_url,
                headers={"X-Stdytime-Pairing-Token": token, "Accept": "application/json"},
                timeout=5,
            )
            payload = resp.json() if resp.headers.get('content-type', '').lower().startswith('application/json') else {}
            if resp.ok and payload.get('ok'):
                return jsonify({
                    'visible': True,
                    'connected': True,
                    'tone': 'success',
                    'label': 'Station Link: connected',
                    'tooltip': f'Scanner linked to Instructor API at {base_url}.',
                }), 200

            reason = payload.get('error') or f'HTTP {resp.status_code}'
            return jsonify({
                'visible': True,
                'connected': False,
                'tone': 'danger',
                'label': 'Station Link: disconnected',
                'tooltip': f'Pairing check failed: {reason}',
            }), 200
        except requests.RequestException as exc:
            return jsonify({
                'visible': True,
                'connected': False,
                'tone': 'danger',
                'label': 'Station Link: offline',
                'tooltip': f'Cannot reach Instructor API: {exc}',
            }), 200

    @app.route('/setup/storage/push-backup', methods=['POST'])
    @require_login
    def setup_storage_push_backup():
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            flash('Cloud push is disabled in Instructor snapshots-only mode.', 'info')
            return redirect(url_for('setup_storage'))
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

    @app.route('/setup/storage/health-check', methods=['POST'])
    @require_login
    def setup_storage_health_check():
        report = get_database_health_report(mode='quick_check')
        if report.get('overall_ok'):
            flash('Database health check passed for available database files.', 'success')
        else:
            flash('Database health check found issues. Review local/cloud/peer status.', 'warning')
        return redirect(url_for('setup_storage'))

    @app.route('/api/db/health', methods=['GET'])
    @require_login
    def api_db_health():
        mode = (request.args.get('mode') or 'quick_check').strip().lower()
        if mode not in {'quick_check', 'integrity_check'}:
            mode = 'quick_check'
        report = get_database_health_report(mode=mode)
        return jsonify(report), 200

    @app.route('/setup/storage/backup-local', methods=['POST'])
    @require_login
    def setup_storage_backup_local():
        result = backup_database_now(source='local', label='setup_manual')
        if result.get('ok'):
            flash(f"Local DB snapshot created: {result.get('path')}", 'success')
        else:
            flash(f"Local DB snapshot failed: {result.get('error') or 'unknown error'}", 'warning')
        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/backup-cloud', methods=['POST'])
    @require_login
    def setup_storage_backup_cloud():
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            flash('Cloud snapshots are disabled in Instructor snapshots-only mode.', 'info')
            return redirect(url_for('setup_storage'))
        result = backup_database_now(source='cloud', label='setup_manual')
        if result.get('ok'):
            flash(f"Cloud DB snapshot created: {result.get('path')}", 'success')
        else:
            flash(f"Cloud DB snapshot failed: {result.get('error') or 'unknown error'}", 'warning')
        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/restore-local-from-cloud', methods=['POST'])
    @require_login
    def setup_storage_restore_local_from_cloud():
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            flash('Cloud restore is disabled in Instructor snapshots-only mode.', 'info')
            return redirect(url_for('setup_storage'))
        result = restore_database_now(target='local', source='cloud')
        if result.get('ok'):
            flash('Restore completed: cloud backup copied to local DB.', 'success')
        else:
            flash(f"Restore failed: {result.get('error') or 'unknown error'}", 'warning')
        return redirect(url_for('setup_storage'))

    @app.route('/setup/storage/restore-cloud-from-local', methods=['POST'])
    @require_login
    def setup_storage_restore_cloud_from_local():
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            flash('Cloud restore is disabled in Instructor snapshots-only mode.', 'info')
            return redirect(url_for('setup_storage'))
        result = restore_database_now(target='cloud', source='local')
        if result.get('ok'):
            flash('Restore completed: local DB copied to cloud backup.', 'success')
        else:
            flash(f"Restore failed: {result.get('error') or 'unknown error'}", 'warning')
        return redirect(url_for('setup_storage'))

    @app.route('/api/cloud/push-backup', methods=['POST'])
    @require_login
    def api_cloud_push_backup():
        """Trigger an immediate best-effort cloud backup push from client-side UI flows."""
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            return jsonify({
                "success": False,
                "pushed": False,
                "error": "Cloud push disabled in Instructor snapshots-only mode.",
            }), 200

        pushed = sync_to_gdrive_now()
        if pushed:
            return jsonify({"success": True, "pushed": True}), 200

        detail = get_last_sync_error()
        return jsonify({
            "success": False,
            "pushed": False,
            "error": detail or "Backup push skipped or failed.",
        }), 200

    @app.route('/setup/storage/pull-backup', methods=['POST'])
    @require_login
    def setup_storage_pull_backup():
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            flash('Cloud pull is disabled in Instructor snapshots-only mode.', 'info')
            return redirect(url_for('setup_storage'))
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

    @app.route('/setup/storage/force-sync-to-cloud', methods=['POST'])
    @require_login
    def setup_storage_force_sync_to_cloud():
        """Force WAL checkpoint (merge to main DB) then sync to OneDrive.
        
        This ensures the main .db file on OneDrive gets updated with current timestamp.
        """
        runtime = get_station_runtime_config()
        if runtime.get('backup_mode') == 'instructor_snapshots_only':
            flash('Force cloud sync is disabled in Instructor snapshots-only mode.', 'info')
            return redirect(url_for('setup_storage'))

        # Step 1: Checkpoint WAL with TRUNCATE to merge pending writes into main DB and shrink WAL file
        checkpoint_ok = run_manual_wal_checkpoint("TRUNCATE")
        if not checkpoint_ok:
            flash('Force sync failed: could not checkpoint WAL.', 'warning')
            return redirect(url_for('setup_storage'))
        
        # Step 2: Push the updated main DB to OneDrive
        pushed = sync_to_gdrive_now()
        if pushed:
            flash('Force sync completed: WAL merged and main DB synced to OneDrive with current timestamp.', 'success')
        else:
            detail = get_last_sync_error()
            if detail:
                flash(f'Force sync failed: {detail}', 'warning')
            else:
                flash('Force sync failed. Verify cloud backup path is configured and accessible.', 'warning')
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
