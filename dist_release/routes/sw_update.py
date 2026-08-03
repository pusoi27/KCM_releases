from __future__ import annotations

from flask import jsonify, render_template, request, url_for
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests

from modules import ls_license
from routes.auth import require_admin, require_login


_RELEASE_ASSET_PATTERN = re.compile(r'^stdytime_installer_v(?P<safe>\d+(?:_\d+)*)\.zip$', re.IGNORECASE)
_SHA256_HEX_PATTERN = re.compile(r'^[0-9a-fA-F]{64}$')
_ACTIVE_UPDATE_STATUSES = {
    'checking',
    'downloading',
    'preparing',
    'ready_for_manual_install',
    'awaiting_browser_close',
    'closing_for_manual_install',
}
_UPDATE_LOCK = threading.Lock()
_CONFIRM_INSTALL_EVENT = threading.Event()
_BROWSER_MONITOR_LOCK = threading.Lock()
_BROWSER_MONITOR_STATE = {
    'client_id': '',
    'last_seen': 0.0,
    'closed': False,
    'close_signal_at': 0.0,
    'confirmed_at': 0.0,
}
_UPDATE_STATE = {
    'status': 'idle',
    'message': '',
    'error': '',
    'current_version': '',
    'latest_version': '',
    'asset_name': '',
    'repo_url': '',
    'source': '',
    'debug_log_path': '',
    'download_dir': '',
    'installer_name': '',
    'installer_path': '',
    'release_notes': '',
    'started_at': 0.0,
    'updated_at': 0.0,
}


class SWUpdateError(RuntimeError):
    """Typed updater error with stable code and retry hint."""

    def __init__(self, message: str, *, code: str = 'unknown_error', retryable: bool = False):
        super().__init__(message)
        self.code = str(code or 'unknown_error').strip()
        self.retryable = bool(retryable)


@dataclass
class UpdateContext:
    current_version: str
    latest_version: str
    asset_name: str
    repo_url: str
    source: str
    debug_log_path: str


def _sw_error(message: str, *, code: str, retryable: bool = False) -> SWUpdateError:
    return SWUpdateError(message, code=code, retryable=retryable)


def _update_error_code(exc: Exception) -> str:
    if isinstance(exc, SWUpdateError):
        return exc.code
    return 'unknown_error'


def _set_update_state(**fields) -> dict:
    with _UPDATE_LOCK:
        _UPDATE_STATE.update(fields)
        _UPDATE_STATE['updated_at'] = time.time()
        return dict(_UPDATE_STATE)


def _get_update_state() -> dict:
    with _UPDATE_LOCK:
        return dict(_UPDATE_STATE)


def _sw_update_debug_log_path() -> Path:
    local_appdata = str(os.getenv('LOCALAPPDATA', '') or '').strip()
    if local_appdata:
        root = Path(local_appdata) / 'Stdytime' / 'logs'
    else:
        root = Path(__file__).resolve().parents[1] / 'logs'
    root.mkdir(parents=True, exist_ok=True)
    return root / 'sw_update_debug.log'


def _sw_update_handoff_path() -> Path:
    return _sw_update_debug_log_path().with_name('sw_update_handoff.json')


def _write_handoff_state(stage: str, **extra) -> None:
    payload = {
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'stage': str(stage or '').strip(),
    }
    payload.update(extra or {})
    try:
        path = _sw_update_handoff_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _sw_update_debug_log(message: str) -> None:
    try:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {str(message or '').strip()}\n"
        with _sw_update_debug_log_path().open('a', encoding='utf-8') as handle:
            handle.write(line)
    except Exception:
        pass


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value or '').strip().lower()
    if not raw:
        return default
    return raw in {'1', 'true', 'yes', 'on'}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name, str(default)) or str(default)).strip()
    try:
        parsed = int(raw)
    except ValueError:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _manual_install_exit_delay_seconds() -> float:
    """Delay between handoff UI update and hard process exit.

    A short grace period gives the browser enough time to process the final
    confirm-click handler and attempt self-close before backend termination.
    """
    raw = str(os.getenv('SW_UPDATE_MANUAL_EXIT_DELAY_SECONDS', '4.0') or '4.0').strip()
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = 4.0
    return max(1.0, min(30.0, parsed))


def _browser_close_wait_seconds() -> float:
    raw = str(os.getenv('SW_UPDATE_BROWSER_CLOSE_WAIT_SECONDS', '25') or '25').strip()
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = 25.0
    return max(3.0, min(180.0, parsed))


def _browser_close_retry_wait_seconds() -> float:
    raw = str(os.getenv('SW_UPDATE_BROWSER_CLOSE_RETRY_SECONDS', '8') or '8').strip()
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = 8.0
    return max(1.0, min(60.0, parsed))


def _browser_heartbeat_grace_seconds() -> float:
    raw = str(os.getenv('SW_UPDATE_BROWSER_HEARTBEAT_GRACE_SECONDS', '6') or '6').strip()
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = 6.0
    return max(1.5, min(30.0, parsed))


def _reset_browser_monitor_state() -> None:
    with _BROWSER_MONITOR_LOCK:
        _BROWSER_MONITOR_STATE.update({
            'client_id': '',
            'last_seen': 0.0,
            'closed': False,
            'close_signal_at': 0.0,
            'confirmed_at': 0.0,
        })


def _set_browser_confirmed(client_id: str = '') -> None:
    now = time.time()
    with _BROWSER_MONITOR_LOCK:
        clean_id = str(client_id or '').strip()
        if clean_id:
            _BROWSER_MONITOR_STATE['client_id'] = clean_id
            _BROWSER_MONITOR_STATE['last_seen'] = now
        _BROWSER_MONITOR_STATE['confirmed_at'] = now


def _register_browser_heartbeat(client_id: str = '') -> None:
    now = time.time()
    with _BROWSER_MONITOR_LOCK:
        clean_id = str(client_id or '').strip()
        if clean_id:
            _BROWSER_MONITOR_STATE['client_id'] = clean_id
        _BROWSER_MONITOR_STATE['last_seen'] = now


def _register_browser_close_signal(client_id: str = '') -> None:
    now = time.time()
    with _BROWSER_MONITOR_LOCK:
        clean_id = str(client_id or '').strip()
        if clean_id:
            _BROWSER_MONITOR_STATE['client_id'] = clean_id
        _BROWSER_MONITOR_STATE['closed'] = True
        _BROWSER_MONITOR_STATE['close_signal_at'] = now


def _get_browser_monitor_state() -> dict:
    with _BROWSER_MONITOR_LOCK:
        return dict(_BROWSER_MONITOR_STATE)


def _wait_for_browser_close_signal() -> tuple[bool, str]:
    """Wait for explicit close signal or heartbeat-stale inference."""
    grace = _browser_heartbeat_grace_seconds()

    def _attempt(wait_seconds: float) -> tuple[bool, str]:
        wait_for = max(0.5, float(wait_seconds))
        deadline = time.monotonic() + wait_for
        while time.monotonic() < deadline:
            state = _get_browser_monitor_state()
            if bool(state.get('closed')):
                return True, 'explicit_close_signal'

            now = time.time()
            confirmed_at = float(state.get('confirmed_at') or 0.0)
            last_seen = float(state.get('last_seen') or 0.0)

            # Inferred close: after user confirmation, heartbeat stopped for a
            # full grace window.
            if confirmed_at > 0 and last_seen > 0 and last_seen >= confirmed_at:
                stale_for = now - last_seen
                if stale_for >= grace:
                    return True, f'heartbeat_stale_{stale_for:.1f}s'

            time.sleep(0.35)

        return False, 'timeout'

    primary_wait = _browser_close_wait_seconds()
    detected, reason = _attempt(primary_wait)
    if detected:
        return detected, reason

    retry_wait = _browser_close_retry_wait_seconds()
    _sw_update_debug_log(
        f'No browser-close signal after {primary_wait:.1f}s. Retrying for {retry_wait:.1f}s.'
    )
    return _attempt(retry_wait)


def _open_installer_folder_with_retry(installer_path_text: str) -> bool:
    """Open Explorer with installer selected; retry once on immediate failure."""
    for attempt in (1, 2):
        try:
            proc = subprocess.Popen(['explorer', f'/select,{installer_path_text}'])
            time.sleep(0.45)
            rc = proc.poll()
            # explorer may remain running (None) or delegate and exit 0 quickly.
            if rc is None or rc == 0:
                _sw_update_debug_log(
                    f'Opened Explorer to installer (attempt {attempt}): {installer_path_text}'
                )
                return True
            _sw_update_debug_log(
                f'Explorer attempt {attempt} returned code {rc}; retrying if possible.'
            )
        except Exception as exp_exc:
            _sw_update_debug_log(f'Could not open Explorer on attempt {attempt}: {exp_exc}')

        if attempt == 1:
            time.sleep(0.8)

    return False


def _gateway_base_url() -> str:
    return str(os.getenv('SW_UPDATE_GATEWAY_URL', '') or '').strip().rstrip('/')


def _gateway_enabled() -> bool:
    return bool(_gateway_base_url())


def _gateway_timeout_seconds() -> int:
    return _env_int('SW_UPDATE_GATEWAY_TIMEOUT_SECONDS', default=25, minimum=5, maximum=120)


def _update_channel() -> str:
    return str(os.getenv('SW_UPDATE_CHANNEL', 'stable') or 'stable').strip().lower() or 'stable'


def _gateway_static_token() -> str:
    return str(os.getenv('SW_UPDATE_GATEWAY_TOKEN', '') or '').strip()


def _client_proof_secret() -> str:
    return str(os.getenv('SW_UPDATE_CLIENT_PROOF_SECRET', '') or '').strip()


def _allow_direct_github_fallback() -> bool:
    raw = str(os.getenv('SW_UPDATE_ALLOW_DIRECT_GITHUB_FALLBACK', '') or '').strip()
    if not raw:
        # Practical default: if no gateway is configured, use direct repository mode.
        return not _gateway_enabled()
    return _as_bool(raw, default=False)


def _require_checksum_verification() -> bool:
    return _as_bool(os.getenv('SW_UPDATE_REQUIRE_CHECKSUM', 'true'), default=True)


def _require_signature_verification() -> bool:
    return _as_bool(os.getenv('SW_UPDATE_REQUIRE_SIGNATURE', 'false'), default=False)


def _minisign_bin() -> str:
    value = str(os.getenv('SW_UPDATE_MINISIGN_BIN', 'minisign') or 'minisign').strip()
    return value or 'minisign'


def _minisign_public_key() -> str:
    return str(os.getenv('SW_UPDATE_MINISIGN_PUBLIC_KEY', '') or '').strip()


def _normalize_repo_url(raw_url: str) -> str:
    value = str(raw_url or '').strip()
    if not value:
        return 'https://github.com/pusoi27/stdytime_releases'
    return value.rstrip('/')


def _repo_url() -> str:
    return _normalize_repo_url(os.getenv('SW_UPDATE_REPO_URL', 'https://github.com/pusoi27/stdytime_releases'))


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r'\d+', str(value or ''))]
    return tuple(numbers) if numbers else (0,)


def _safe_version_to_display(safe_version: str) -> str:
    return '.'.join(safe_version.split('_'))


def _parse_github_repo(repo_url: str) -> tuple[str, str]:
    match = re.search(r'github\.com/(?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?$', repo_url, re.IGNORECASE)
    if not match:
        raise ValueError(f'Unsupported GitHub repository URL: {repo_url}')
    return match.group('owner'), match.group('repo')


def _build_asset_record(name: str, download_url: str, repo_url: str) -> dict | None:
    match = _RELEASE_ASSET_PATTERN.match(str(name or '').strip())
    if not match:
        return None
    safe_version = match.group('safe')
    return {
        'name': name,
        'safe_version': safe_version,
        'version': _safe_version_to_display(safe_version),
        'version_tuple': tuple(int(part) for part in safe_version.split('_')),
        'download_url': str(download_url or '').strip(),
        'repo_url': repo_url,
        'checksum_url': '',
        'expected_sha256': '',
        'signature_url': '',
        'expected_signature': '',
        'minisign_public_key': '',
    }


def _normalize_sha256_text(value: str) -> str:
    candidate = str(value or '').strip().lower()
    if _SHA256_HEX_PATTERN.fullmatch(candidate):
        return candidate
    return ''


def _extract_sha256_from_text(payload_text: str) -> str:
    for line in str(payload_text or '').splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        normalized = _normalize_sha256_text(parts[0])
        if normalized:
            return normalized
    return ''


def _normalize_signature_text(value: str) -> str:
    text = str(value or '').strip()
    return text if text else ''


def _normalize_release_notes(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        lines = [str(item).strip() for item in value if str(item or '').strip()]
        return '\n'.join(lines).strip()
    return ''


def _set_update_phase(ctx: UpdateContext, *, status: str, message: str, error: str = '', **extra_fields) -> None:
    _set_update_state(
        status=status,
        message=message,
        error=error,
        current_version=ctx.current_version,
        latest_version=ctx.latest_version,
        asset_name=ctx.asset_name,
        repo_url=ctx.repo_url,
        source=ctx.source,
        debug_log_path=ctx.debug_log_path,
        **(extra_fields or {}),
    )


def _identity_snapshot() -> dict:
    try:
        ctx = ls_license.get_ls_license_context() or {}
    except Exception:
        ctx = {}
    return {
        'machine_fingerprint': str(ctx.get('machine_fingerprint') or '').strip(),
        'station_role': str(ctx.get('station_role') or '').strip().lower(),
        'license_status': str(ctx.get('status') or '').strip().lower(),
        'activation_limit': int(ctx.get('activation_limit') or 0),
        'license_email': str(ctx.get('email') or '').strip(),
        'licensee': str(ctx.get('licensee') or '').strip(),
    }


def _build_gateway_headers(identity: dict, current_version: str) -> dict[str, str]:
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'Stdytime-SW-Updater',
        'X-Stdytime-App': 'stdytime',
        'X-Stdytime-Version': str(current_version or '').strip(),
        'X-Stdytime-Channel': _update_channel(),
        'X-Stdytime-Machine': str(identity.get('machine_fingerprint') or ''),
        'X-Stdytime-Station-Role': str(identity.get('station_role') or ''),
        'X-Stdytime-License-Status': str(identity.get('license_status') or ''),
        'X-Stdytime-Activation-Limit': str(identity.get('activation_limit') or 0),
    }
    static_token = _gateway_static_token()
    if static_token:
        headers['Authorization'] = f'Bearer {static_token}'

    proof_secret = _client_proof_secret()
    if proof_secret:
        ts = str(int(time.time()))
        payload = '|'.join([
            str(identity.get('machine_fingerprint') or ''),
            str(current_version or ''),
            _update_channel(),
            ts,
        ])
        signature = hmac.new(
            proof_secret.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        headers['X-Stdytime-Proof-Timestamp'] = ts
        headers['X-Stdytime-Proof'] = signature

    return headers


def _normalize_gateway_download_url(download_url: str) -> str:
    url = str(download_url or '').strip()
    if not url:
        return ''
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return urljoin(f"{_gateway_base_url()}/", url.lstrip('/'))


def _gateway_check_for_update(current_version: str, identity: dict) -> dict:
    base = _gateway_base_url()
    if not base:
        raise _sw_error('SW_UPDATE_GATEWAY_URL is not configured.', code='gateway_not_configured')

    headers = _build_gateway_headers(identity, current_version)
    params = {
        'app': 'stdytime',
        'channel': _update_channel(),
        'current_version': current_version,
    }
    response = requests.get(
        f'{base}/updates/check',
        headers=headers,
        params=params,
        timeout=_gateway_timeout_seconds(),
    )
    response.raise_for_status()

    payload = response.json() if response.content else {}
    if not isinstance(payload, dict):
        raise _sw_error('Gateway returned an invalid check payload.', code='gateway_invalid_payload')
    if payload.get('ok') is False:
        raise _sw_error(str(payload.get('error') or 'Gateway denied update check.'), code='gateway_denied_check')

    latest_version = str(payload.get('latest_version') or '').strip()
    asset_name = str(payload.get('asset_name') or '').strip()
    update_available = bool(payload.get('update_available'))
    if latest_version and _version_tuple(latest_version) <= _version_tuple(current_version):
        update_available = False

    return {
        'current_version': current_version,
        'repo_url': base,
        'source': 'gateway',
        'update_available': update_available,
        'latest_version': latest_version or current_version,
        'asset_name': asset_name,
        'download_url': _normalize_gateway_download_url(str(payload.get('download_url') or '')),
        'download_headers': payload.get('download_headers') if isinstance(payload.get('download_headers'), dict) else {},
        'checksum_url': _normalize_gateway_download_url(str(payload.get('checksum_url') or payload.get('sha256_url') or '')),
        'expected_sha256': _normalize_sha256_text(
            str(payload.get('expected_sha256') or payload.get('sha256') or payload.get('checksum_sha256') or '')
        ),
        'signature_url': _normalize_gateway_download_url(str(payload.get('signature_url') or payload.get('minisign_url') or '')),
        'expected_signature': _normalize_signature_text(str(payload.get('expected_signature') or payload.get('minisign_signature') or '')),
        'minisign_public_key': str(payload.get('minisign_public_key') or '').strip(),
        'ticket_endpoint': str(payload.get('ticket_endpoint') or '').strip(),
        'release_id': str(payload.get('release_id') or '').strip(),
        'release_notes': _normalize_release_notes(
            payload.get('release_notes')
            or payload.get('changelog')
            or payload.get('notes')
            or payload.get('release_body')
            or ''
        ),
        'raw': payload,
    }


def _gateway_request_download_ticket(update_result: dict, identity: dict) -> dict:
    base = _gateway_base_url()
    endpoint = str(update_result.get('ticket_endpoint') or '').strip()
    ticket_url = _normalize_gateway_download_url(endpoint) if endpoint else f'{base}/updates/ticket'

    current_version = str(update_result.get('current_version') or '').strip()
    headers = _build_gateway_headers(identity, current_version)
    headers['Content-Type'] = 'application/json'

    payload = {
        'app': 'stdytime',
        'channel': _update_channel(),
        'current_version': current_version,
        'latest_version': str(update_result.get('latest_version') or '').strip(),
        'asset_name': str(update_result.get('asset_name') or '').strip(),
        'release_id': str(update_result.get('release_id') or '').strip(),
        'machine_fingerprint': str(identity.get('machine_fingerprint') or ''),
        'station_role': str(identity.get('station_role') or ''),
        'license_status': str(identity.get('license_status') or ''),
    }

    response = requests.post(ticket_url, headers=headers, json=payload, timeout=_gateway_timeout_seconds())
    response.raise_for_status()

    ticket = response.json() if response.content else {}
    if not isinstance(ticket, dict):
        raise _sw_error('Gateway returned an invalid ticket payload.', code='gateway_invalid_ticket')
    if ticket.get('ok') is False:
        raise _sw_error(str(ticket.get('error') or 'Gateway denied download ticket request.'), code='gateway_denied_ticket')

    download_url = _normalize_gateway_download_url(str(ticket.get('download_url') or ''))
    if not download_url:
        raise _sw_error('Gateway ticket response did not include download_url.', code='ticket_missing_download_url', retryable=True)

    extra_headers = ticket.get('download_headers') if isinstance(ticket.get('download_headers'), dict) else {}
    return {
        'download_url': download_url,
        'download_headers': {str(k): str(v) for k, v in extra_headers.items()},
        'checksum_url': _normalize_gateway_download_url(str(ticket.get('checksum_url') or ticket.get('sha256_url') or '')),
        'expected_sha256': _normalize_sha256_text(
            str(ticket.get('expected_sha256') or ticket.get('sha256') or ticket.get('checksum_sha256') or '')
        ),
        'signature_url': _normalize_gateway_download_url(str(ticket.get('signature_url') or ticket.get('minisign_url') or '')),
        'expected_signature': _normalize_signature_text(str(ticket.get('expected_signature') or ticket.get('minisign_signature') or '')),
        'minisign_public_key': str(ticket.get('minisign_public_key') or '').strip(),
        'release_notes': _normalize_release_notes(
            ticket.get('release_notes')
            or ticket.get('changelog')
            or ticket.get('notes')
            or ticket.get('release_body')
            or ''
        ),
    }


def _github_headers() -> dict[str, str]:
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'Stdytime-SW-Updater',
    }
    token = str(os.getenv('SW_UPDATE_GITHUB_TOKEN', '') or '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _github_download_headers() -> dict[str, str]:
    headers = {'User-Agent': 'Stdytime-SW-Updater'}
    token = str(os.getenv('SW_UPDATE_GITHUB_TOKEN', '') or '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'
        headers['Accept'] = 'application/octet-stream'
    return headers


def _list_release_assets_via_api(repo_url: str) -> list[dict]:
    owner, repo = _parse_github_repo(repo_url)
    releases_url = f'https://api.github.com/repos/{owner}/{repo}/releases?per_page=40'
    response = requests.get(releases_url, headers=_github_headers(), timeout=_gateway_timeout_seconds())
    if response.status_code == 404:
        raise FileNotFoundError(f'GitHub repository not reachable: {repo_url}')
    response.raise_for_status()

    payload = response.json()
    releases = payload if isinstance(payload, list) else []
    has_token = bool(str(os.getenv('SW_UPDATE_GITHUB_TOKEN', '') or '').strip())
    assets: list[dict] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if bool(release.get('draft')):
            continue
        if _update_channel() == 'stable' and bool(release.get('prerelease')):
            continue

        release_notes = _normalize_release_notes(release.get('body') or '')

        release_assets = release.get('assets') if isinstance(release.get('assets'), list) else []
        download_by_name: dict[str, str] = {}
        for item in release_assets:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get('name') or '').strip()
            browser_download_url = str(item.get('browser_download_url') or '').strip()
            api_download_url = str(item.get('url') or '').strip()
            selected_download_url = api_download_url if has_token and api_download_url else browser_download_url
            if item_name and selected_download_url:
                download_by_name[item_name.lower()] = selected_download_url

        for item in release_assets:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get('name') or '').strip()
            browser_download_url = str(item.get('browser_download_url') or '').strip()
            api_download_url = str(item.get('url') or '').strip()
            selected_download_url = api_download_url if has_token and api_download_url else browser_download_url
            if not item_name or not selected_download_url:
                continue
            asset = _build_asset_record(item_name, selected_download_url, repo_url)
            if not asset:
                continue

            checksum_name = f"{asset['name']}.sha256"
            signature_name = f"{asset['name']}.minisig"
            asset['checksum_url'] = str(download_by_name.get(checksum_name.lower()) or '').strip()
            asset['signature_url'] = str(download_by_name.get(signature_name.lower()) or '').strip()
            asset['release_notes'] = release_notes
            assets.append(asset)

    return assets


def _find_latest_release_asset(current_version: str, repo_url: str) -> tuple[dict | None, list[dict]]:
    assets = _list_release_assets_via_api(repo_url)
    if not assets:
        raise RuntimeError('No release ZIP files were found in fallback GitHub source.')
    assets.sort(key=lambda item: item['version_tuple'], reverse=True)
    latest = assets[0]
    if tuple(latest['version_tuple']) <= tuple(_version_tuple(current_version)):
        return None, assets
    return latest, assets


def _legacy_check_for_update(current_version: str) -> dict:
    repo = _repo_url()
    latest_asset, _assets = _find_latest_release_asset(current_version, repo)
    if not latest_asset:
        return {
            'current_version': current_version,
            'repo_url': repo,
            'source': 'github-fallback',
            'update_available': False,
            'latest_version': current_version,
            'asset_name': '',
            'download_url': '',
            'download_headers': {},
            'checksum_url': '',
            'expected_sha256': '',
            'signature_url': '',
            'expected_signature': '',
            'minisign_public_key': '',
            'ticket_endpoint': '',
            'release_id': '',
            'release_notes': '',
            'raw': {},
        }

    return {
        'current_version': current_version,
        'repo_url': repo,
        'source': 'github-fallback',
        'update_available': True,
        'latest_version': latest_asset['version'],
        'asset_name': latest_asset['name'],
        'download_url': str(latest_asset.get('download_url') or '').strip(),
        'download_headers': _github_download_headers(),
        'checksum_url': str(latest_asset.get('checksum_url') or '').strip(),
        'expected_sha256': '',
        'signature_url': str(latest_asset.get('signature_url') or '').strip(),
        'expected_signature': '',
        'minisign_public_key': '',
        'ticket_endpoint': '',
        'release_id': '',
        'release_notes': _normalize_release_notes(latest_asset.get('release_notes') or ''),
        'raw': latest_asset,
    }


def _check_for_update(current_version: str, identity: dict) -> dict:
    if _gateway_enabled():
        return _gateway_check_for_update(current_version, identity)

    if _allow_direct_github_fallback():
        return _legacy_check_for_update(current_version)

    raise _sw_error(
        'Software update source is not configured. '
        'Set SW_UPDATE_REPO_URL for direct mode or SW_UPDATE_GATEWAY_URL for gateway mode.',
        code='update_source_not_configured'
    )


def _resolve_install_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent

    local_appdata = os.getenv('LOCALAPPDATA', '').strip()
    if local_appdata:
        return Path(local_appdata) / 'Stdytime'

    return Path(__file__).resolve().parents[1]


def _resolve_update_download_root() -> Path:
    local_appdata = str(os.getenv('LOCALAPPDATA', '') or '').strip()
    if local_appdata:
        root = Path(local_appdata) / 'Stdytime' / 'sw_update_downloads'
    else:
        root = Path(__file__).resolve().parents[1] / 'sw_update_downloads'
    root.mkdir(parents=True, exist_ok=True)
    return root


def _find_installer_executable(package_dir: Path) -> Path | None:
    candidates = [path for path in package_dir.rglob('*.exe') if path.is_file()]
    if not candidates:
        return None

    def _score(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        if name.startswith('stdytime_installer'):
            priority = 0
        elif 'installer' in name:
            priority = 1
        elif name == 'stdytime.exe':
            priority = 2
        else:
            priority = 3
        return (priority, len(path.parts), name)

    candidates.sort(key=_score)
    return candidates[0]


def _stage_manual_update_payload(extract_dir: Path, *, asset_name: str, latest_version: str) -> tuple[Path, Path, Path]:
    root = _resolve_update_download_root()
    stamp = time.strftime('%Y%m%d_%H%M%S')
    default_name = f'stdytime_update_{latest_version or "latest"}'
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(asset_name or default_name).stem).strip('._-') or default_name
    target_dir = root / f'{stamp}_{safe_name}'
    suffix = 1
    while target_dir.exists():
        suffix += 1
        target_dir = root / f'{stamp}_{safe_name}_{suffix}'

    package_dir = target_dir / 'package'
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(extract_dir, package_dir)

    installer_path = _find_installer_executable(package_dir)
    if installer_path is None:
        raise _sw_error(
            'Downloaded package does not contain a launchable Windows executable (.exe).',
            code='installer_exe_missing',
        )

    return target_dir, package_dir, installer_path


def _verify_minisign_signature(
    zip_path: Path,
    *,
    signature_text: str = '',
    signature_url: str = '',
    headers: dict | None = None,
    public_key: str = '',
) -> None:
    pubkey = str(public_key or '').strip()
    if not pubkey:
        raise _sw_error('SW_UPDATE_MINISIGN_PUBLIC_KEY is required for minisign verification.', code='minisign_pubkey_missing')

    signature_payload = _normalize_signature_text(signature_text)
    if not signature_payload and signature_url:
        sig_response = requests.get(
            signature_url,
            headers=headers or {'User-Agent': 'Stdytime-SW-Updater'},
            timeout=_gateway_timeout_seconds(),
        )
        if sig_response.status_code in {401, 403}:
            raise _sw_error('Signature file access was denied by the update source.', code='signature_access_denied')
        if sig_response.status_code == 404:
            raise _sw_error('Signature file was not found for the update payload.', code='signature_sidecar_not_found', retryable=True)
        sig_response.raise_for_status()
        signature_payload = _normalize_signature_text(sig_response.text)

    if not signature_payload:
        raise _sw_error('No minisign signature payload is available for this update.', code='signature_missing')

    sig_path = zip_path.with_suffix(zip_path.suffix + '.minisig')
    sig_path.write_text(signature_payload, encoding='utf-8')

    try:
        verify = subprocess.run(
            [
                _minisign_bin(),
                '-Vm', str(zip_path),
                '-P', pubkey,
                '-x', str(sig_path),
            ],
            capture_output=True,
            text=True,
            timeout=max(10, _gateway_timeout_seconds()),
            check=False,
        )
    except FileNotFoundError as exc:
        raise _sw_error(
            f"minisign executable '{_minisign_bin()}' was not found. Install minisign or set SW_UPDATE_MINISIGN_BIN."
            , code='minisign_not_found'
        ) from exc

    if verify.returncode != 0:
        details = (verify.stderr or verify.stdout or '').strip()
        raise _sw_error(f'Minisign verification failed. {details}', code='signature_verify_failed')


def _download_release_zip(
    download_url: str,
    *,
    asset_name: str = 'stdytime_update.zip',
    extra_headers: dict | None = None,
    expected_sha256: str = '',
    checksum_url: str = '',
    expected_signature: str = '',
    signature_url: str = '',
    minisign_public_key: str = '',
) -> Path:
    final_url = str(download_url or '').strip()
    if not final_url:
        raise _sw_error('Update payload URL is missing.', code='download_url_missing', retryable=True)

    _sw_update_debug_log(f'Download step started. URL={final_url}')

    headers = {'User-Agent': 'Stdytime-SW-Updater'}
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if key and value is not None:
                headers[str(key)] = str(value)
    headers.setdefault('Cache-Control', 'no-cache, no-store, must-revalidate')
    headers.setdefault('Pragma', 'no-cache')
    headers.setdefault('Expires', '0')

    response = requests.get(
        final_url,
        headers=headers,
        timeout=(_gateway_timeout_seconds(), 180),
        stream=True,
    )
    _sw_update_debug_log(f'Download response status={response.status_code} for URL={final_url}')
    if response.status_code in {401, 403}:
        raise _sw_error('Update download was denied by the update server.', code='download_access_denied')
    if response.status_code == 404:
        raise _sw_error('Update payload is unavailable (404).', code='download_not_found', retryable=True)
    response.raise_for_status()

    temp_dir = Path(tempfile.mkdtemp(prefix='stdytime_sw_update_'))
    zip_path = temp_dir / (asset_name or 'stdytime_update.zip')
    digest = hashlib.sha256()
    with zip_path.open('wb') as handle:
        total_bytes = 0
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
                digest.update(chunk)
                total_bytes += len(chunk)
    _sw_update_debug_log(f'Download complete. bytes={total_bytes} file={zip_path}')

    inline_expected_hash = _normalize_sha256_text(expected_sha256)
    checksum_expected_hash = ''
    if checksum_url:
        _sw_update_debug_log(f'Checksum fetch started. URL={checksum_url}')
        checksum_response = requests.get(
            checksum_url,
            headers=headers,
            timeout=_gateway_timeout_seconds(),
        )
        _sw_update_debug_log(f'Checksum response status={checksum_response.status_code} URL={checksum_url}')
        if checksum_response.status_code in {401, 403}:
            raise _sw_error('Checksum file access was denied by the update source.', code='checksum_access_denied')
        if checksum_response.status_code == 404:
            raise _sw_error('Checksum file was not found for the update payload.', code='checksum_sidecar_not_found', retryable=True)
        checksum_response.raise_for_status()
        checksum_expected_hash = _extract_sha256_from_text(checksum_response.text)

    # Prefer checksum sidecar hash when available to avoid stale inline hashes from
    # intermediate update metadata caches.
    resolved_expected_hash = checksum_expected_hash or inline_expected_hash

    if _require_checksum_verification() and not resolved_expected_hash:
        raise _sw_error(
            'Checksum verification required, but no valid SHA-256 value is available for this update.',
            code='checksum_required_missing',
        )

    if resolved_expected_hash:
        downloaded_hash = digest.hexdigest().lower()
        if downloaded_hash != resolved_expected_hash:
            raise _sw_error(
                f'Checksum verification failed for {asset_name}: expected {resolved_expected_hash}, got {downloaded_hash}.'
                , code='checksum_mismatch', retryable=True
            )
        _sw_update_debug_log(f'Checksum verification passed for {asset_name}. hash={downloaded_hash}')

    signature_payload = _normalize_signature_text(expected_signature)
    signature_source_url = str(signature_url or '').strip()
    public_key = str(minisign_public_key or _minisign_public_key() or '').strip()

    if _require_signature_verification() and not (signature_payload or signature_source_url):
        raise _sw_error(
            'Signature verification is required, but no minisign signature is available for this update.',
            code='signature_required_missing',
        )

    should_verify_signature = bool(signature_payload or signature_source_url or _require_signature_verification())
    if should_verify_signature:
        _sw_update_debug_log(f'Signature verification started for {asset_name}. signature_url={signature_source_url or "<inline>"}')
        _verify_minisign_signature(
            zip_path,
            signature_text=signature_payload,
            signature_url=signature_source_url,
            headers=headers,
            public_key=public_key,
        )
        _sw_update_debug_log(f'Signature verification passed for {asset_name}.')

    if not zipfile.is_zipfile(zip_path):
        raise _sw_error('Downloaded update is not a valid ZIP archive.', code='download_not_zip')

    extract_dir = temp_dir / 'payload'
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as archive:
        archive.extractall(extract_dir)

    _sw_update_debug_log(f'Payload extracted to {extract_dir}')

    return extract_dir


def _detect_payload_root(extract_dir: Path) -> Path:
    markers = {'VERSION', 'launcher.py', 'Stdytime.exe', 'app.py'}
    candidates: list[Path] = []

    for root, _dirs, files in os.walk(extract_dir):
        file_names = set(files)
        if 'VERSION' in file_names and file_names.intersection(markers - {'VERSION'}):
            candidates.append(Path(root))

    if candidates:
        candidates.sort(key=lambda item: len(str(item)))
        return candidates[0]

    child_dirs = [path for path in extract_dir.iterdir() if path.is_dir()]
    if len(child_dirs) == 1:
        return child_dirs[0]

    return extract_dir


def _write_update_helper_script(source_dir: Path, install_dir: Path, wait_pid: int, debug_log_path: Path) -> Path:
    helper_dir = Path(tempfile.mkdtemp(prefix='stdytime_sw_apply_'))
    helper_path = helper_dir / 'apply_sw_update.ps1'
    template_path = Path(__file__).resolve().parents[1] / 'assets' / 'sw_update' / 'apply_sw_update.ps1.template'
    if not template_path.exists():
        raise _sw_error(f'Update helper template is missing: {template_path}', code='helper_template_missing')
    helper_path.write_text(template_path.read_text(encoding='utf-8'), encoding='utf-8')
    return helper_path


def _launch_update_helper(source_dir: Path, install_dir: Path, wait_pid: int) -> None:
    debug_log_path = _sw_update_debug_log_path()
    handoff_path = _sw_update_handoff_path()
    helper_path = _write_update_helper_script(source_dir, install_dir, wait_pid, debug_log_path)
    creation_flags = 0
    if os.name == 'nt':
        creation_flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

    subprocess.Popen(
        [
            'powershell.exe',
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', str(helper_path),
            '-WaitPid', str(wait_pid),
            '-SourceDir', str(source_dir),
            '-InstallDir', str(install_dir),
            '-DebugLogPath', str(debug_log_path),
            '-HandoffPath', str(handoff_path),
        ],
        cwd=str(install_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
    )
    _sw_update_debug_log(
        f'Update helper launched. helper_script={helper_path} source={source_dir} install={install_dir} wait_pid={wait_pid} debug_log={debug_log_path}'
    )
    _write_handoff_state(
        'helper_launch_dispatched',
        helper_script=str(helper_path),
        source=str(source_dir),
        install=str(install_dir),
        wait_pid=int(wait_pid),
    )


def _resolve_download_spec(update_result: dict, identity: dict) -> dict:
    download_url = str(update_result.get('download_url') or '').strip()
    headers = update_result.get('download_headers') if isinstance(update_result.get('download_headers'), dict) else {}
    expected_sha256 = _normalize_sha256_text(str(update_result.get('expected_sha256') or ''))
    checksum_url = str(update_result.get('checksum_url') or '').strip()
    expected_signature = _normalize_signature_text(str(update_result.get('expected_signature') or ''))
    signature_url = str(update_result.get('signature_url') or '').strip()
    minisign_public_key = str(update_result.get('minisign_public_key') or '').strip()
    if download_url:
        return {
            'download_url': download_url,
            'download_headers': {str(k): str(v) for k, v in headers.items()},
            'expected_sha256': expected_sha256,
            'checksum_url': checksum_url,
            'expected_signature': expected_signature,
            'signature_url': signature_url,
            'minisign_public_key': minisign_public_key,
            'release_notes': _normalize_release_notes(update_result.get('release_notes') or ''),
        }

    if update_result.get('source') == 'gateway':
        ticket = _gateway_request_download_ticket(update_result, identity)
        return {
            'download_url': str(ticket.get('download_url') or '').strip(),
            'download_headers': ticket.get('download_headers') if isinstance(ticket.get('download_headers'), dict) else {},
            'expected_sha256': _normalize_sha256_text(
                str(ticket.get('expected_sha256') or expected_sha256 or '')
            ),
            'checksum_url': str(ticket.get('checksum_url') or checksum_url or '').strip(),
            'expected_signature': _normalize_signature_text(
                str(ticket.get('expected_signature') or expected_signature or '')
            ),
            'signature_url': str(ticket.get('signature_url') or signature_url or '').strip(),
            'minisign_public_key': str(ticket.get('minisign_public_key') or minisign_public_key or '').strip(),
            'release_notes': _normalize_release_notes(
                ticket.get('release_notes')
                or update_result.get('release_notes')
                or ''
            ),
        }

    raise _sw_error('No downloadable payload URL is available for this update source.', code='download_url_unavailable', retryable=True)


def _should_retry_download_resolution(exc: RuntimeError, update_result: dict) -> bool:
    if update_result.get('source') != 'gateway':
        return False

    if isinstance(exc, SWUpdateError):
        return bool(exc.retryable)

    message = str(exc or '')
    retry_markers = (
        'Checksum verification failed',
        'Update payload is unavailable (404).',
        'Update payload URL is missing.',
        'Checksum file was not found for the update payload.',
        'Signature file was not found for the update payload.',
    )
    return any(marker in message for marker in retry_markers)


def register_sw_update_routes(app, get_app_version_func):
    def _background_install_worker(identity: dict) -> None:
        try:
            current_version = get_app_version_func()
            debug_log_path = str(_sw_update_debug_log_path())
            _write_handoff_state('worker_started', source='sw_update_worker')
            _sw_update_debug_log('SW update worker started.')
            result = _check_for_update(current_version, identity)
            _sw_update_debug_log(
                f"Update check complete. update_available={bool(result.get('update_available'))} "
                f"latest_version={result.get('latest_version') or ''} source={result.get('source') or ''}"
            )

            ctx = UpdateContext(
                current_version=current_version,
                latest_version=str(result.get('latest_version') or ''),
                asset_name=str(result.get('asset_name') or ''),
                repo_url=str(result.get('repo_url') or ''),
                source=str(result.get('source') or ''),
                debug_log_path=debug_log_path,
            )

            if not result.get('update_available'):
                ctx.latest_version = str(result.get('latest_version') or current_version)
                _set_update_phase(
                    ctx,
                    status='idle',
                    message=f'No newer software update found. Current version {current_version} is already up to date.',
                    download_dir='',
                    installer_name='',
                    installer_path='',
                    release_notes='',
                )
                return

            _set_update_phase(
                ctx,
                status='downloading',
                message=f"Downloading {ctx.asset_name or 'software update'} from update source...",
                download_dir='',
                installer_name='',
                installer_path='',
                release_notes='',
            )
            _sw_update_debug_log(f"Download phase entered for asset={result.get('asset_name') or ''}")

            download_spec = _resolve_download_spec(result, identity)
            _sw_update_debug_log(
                f"Download spec resolved. url={download_spec.get('download_url') or ''} "
                f"checksum_url={download_spec.get('checksum_url') or ''} signature_url={download_spec.get('signature_url') or ''}"
            )
            try:
                extract_dir = _download_release_zip(
                    download_spec.get('download_url') or '',
                    asset_name=str(result.get('asset_name') or 'stdytime_update.zip'),
                    extra_headers=download_spec.get('download_headers') if isinstance(download_spec.get('download_headers'), dict) else None,
                    expected_sha256=str(download_spec.get('expected_sha256') or ''),
                    checksum_url=str(download_spec.get('checksum_url') or ''),
                    expected_signature=str(download_spec.get('expected_signature') or ''),
                    signature_url=str(download_spec.get('signature_url') or ''),
                    minisign_public_key=str(download_spec.get('minisign_public_key') or ''),
                )
            except RuntimeError as first_exc:
                if not _should_retry_download_resolution(first_exc, result):
                    raise

                retry_reason = 'download metadata appears stale'
                first_message = str(first_exc)
                if 'Checksum verification failed' in first_message:
                    retry_reason = 'checksum mismatch detected'
                elif 'Update payload is unavailable (404).' in first_message:
                    retry_reason = 'update payload returned 404'
                elif 'Update payload URL is missing.' in first_message:
                    retry_reason = 'download URL is missing'
                elif 'Checksum file was not found for the update payload.' in first_message:
                    retry_reason = 'checksum sidecar returned 404'
                elif 'Signature file was not found for the update payload.' in first_message:
                    retry_reason = 'signature sidecar returned 404'

                # Transient cache/race hardening: refresh ticket/spec once and retry.
                _set_update_phase(
                    ctx,
                    status='downloading',
                    message=f'{retry_reason.capitalize()}. Refreshing download ticket and retrying once...',
                )
                _sw_update_debug_log(f'Retry triggered. reason={retry_reason}. First error={first_message}')

                download_spec = _resolve_download_spec(result, identity)
                _sw_update_debug_log(
                    f"Retry download spec resolved. url={download_spec.get('download_url') or ''} "
                    f"checksum_url={download_spec.get('checksum_url') or ''} signature_url={download_spec.get('signature_url') or ''}"
                )
                extract_dir = _download_release_zip(
                    download_spec.get('download_url') or '',
                    asset_name=str(result.get('asset_name') or 'stdytime_update.zip'),
                    extra_headers=download_spec.get('download_headers') if isinstance(download_spec.get('download_headers'), dict) else None,
                    expected_sha256=str(download_spec.get('expected_sha256') or ''),
                    checksum_url=str(download_spec.get('checksum_url') or ''),
                    expected_signature=str(download_spec.get('expected_signature') or ''),
                    signature_url=str(download_spec.get('signature_url') or ''),
                    minisign_public_key=str(download_spec.get('minisign_public_key') or ''),
                )

            _set_update_phase(
                ctx,
                status='preparing',
                message='Preparing downloaded package for manual installation...',
            )
            _sw_update_debug_log(f'Preparing phase entered. extract_dir={extract_dir}')

            download_dir, package_dir, installer_path = _stage_manual_update_payload(
                extract_dir,
                asset_name=str(result.get('asset_name') or ''),
                latest_version=ctx.latest_version,
            )
            _sw_update_debug_log(
                f'Manual install package staged. download_dir={download_dir} package_dir={package_dir} installer={installer_path}'
            )

            installer_name = installer_path.name
            installer_path_text = str(installer_path)
            download_dir_text = str(download_dir)

            _set_update_phase(
                ctx,
                status='ready_for_manual_install',
                message=(
                    'Download complete. Confirm the prompt, then close this browser window to continue.'
                ),
                download_dir=download_dir_text,
                installer_name=installer_name,
                installer_path=installer_path_text,
                release_notes=_normalize_release_notes(
                    download_spec.get('release_notes')
                    or result.get('release_notes')
                    or ''
                ),
            )
            _write_handoff_state(
                'manual_install_ready',
                download_dir=download_dir_text,
                package_dir=str(package_dir),
                installer_name=installer_name,
                installer_path=installer_path_text,
            )

            _sw_update_debug_log('Waiting for user confirmation to open installer folder and close...')
            confirmed = _CONFIRM_INSTALL_EVENT.wait(timeout=600.0)
            if not confirmed:
                _sw_update_debug_log('User confirmation timed out after 10 minutes; continuing to wait for explicit confirmation.')
                while not _CONFIRM_INSTALL_EVENT.wait(timeout=30.0):
                    _sw_update_debug_log('Still waiting for user confirmation to continue manual install handoff...')

            _set_update_phase(
                ctx,
                status='awaiting_browser_close',
                message='Please close this browser window now. Stdytime will continue once closure is detected.',
                download_dir=download_dir_text,
                installer_name=installer_name,
                installer_path=installer_path_text,
                release_notes=_normalize_release_notes(
                    download_spec.get('release_notes')
                    or result.get('release_notes')
                    or ''
                ),
            )
            close_detected = False
            close_reason = 'not_checked'
            wait_round = 0
            while not close_detected:
                close_detected, close_reason = _wait_for_browser_close_signal()
                if close_detected:
                    _sw_update_debug_log(f'Browser closure detected. reason={close_reason}')
                    break

                wait_round += 1
                _sw_update_debug_log(
                    f'Browser closure not detected yet (round {wait_round}); continuing to wait before opening installer folder.'
                )
                _set_update_phase(
                    ctx,
                    status='awaiting_browser_close',
                    message='Waiting for browser window to close. Installer folder will open immediately after closure is detected.',
                    download_dir=download_dir_text,
                    installer_name=installer_name,
                    installer_path=installer_path_text,
                    release_notes=_normalize_release_notes(
                        download_spec.get('release_notes')
                        or result.get('release_notes')
                        or ''
                    ),
                )

            _set_update_phase(
                ctx,
                status='closing_for_manual_install',
                message=(
                    f'Opening installer folder and closing Stdytime now...'
                ),
                download_dir=download_dir_text,
                installer_name=installer_name,
                installer_path=installer_path_text,
                release_notes=_normalize_release_notes(
                    download_spec.get('release_notes')
                    or result.get('release_notes')
                    or ''
                ),
            )
            _sw_update_debug_log('User confirmed. Beginning graceful shutdown of current app instance.')

            _write_handoff_state('manual_install_shutdown', installer_name=installer_name, installer_path=installer_path_text)
            explorer_opened = _open_installer_folder_with_retry(installer_path_text)
            _write_handoff_state(
                'manual_install_explorer_dispatch',
                installer_name=installer_name,
                installer_path=installer_path_text,
                explorer_opened=bool(explorer_opened),
            )
            if not explorer_opened:
                _sw_update_debug_log('Explorer open could not be verified; continuing shutdown fallback.')
            exit_delay = _manual_install_exit_delay_seconds()
            _sw_update_debug_log(f'Waiting {exit_delay:.1f}s before process exit to allow browser close handoff.')
            time.sleep(exit_delay)
            _sw_update_debug_log('Calling os._exit(0) after manual installer handoff.')
            os._exit(0)
        except Exception as exc:
            _sw_update_debug_log(f'Worker failed with exception: {exc}')
            _set_update_state(
                status='error',
                message='Software update failed.',
                error=f'[{_update_error_code(exc)}] {exc}',
                debug_log_path=str(_sw_update_debug_log_path()),
                installer_name='',
                installer_path='',
                release_notes='',
            )
            _write_handoff_state('worker_failed', error=str(exc), code=_update_error_code(exc))

    def _read_debug_log_tail(max_lines: int = 120) -> list[str]:
        path = _sw_update_debug_log_path()
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
            return lines[-max(1, int(max_lines or 120)):]
        except Exception:
            return []

    def _read_handoff_state() -> dict:
        path = _sw_update_handoff_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @app.route('/api/sw-update/check', methods=['GET'])
    @require_login
    def sw_update_check_api():
        state = _get_update_state()
        if state.get('status') in _ACTIVE_UPDATE_STATUSES:
            return jsonify({
                'ok': True,
                'update_available': True,
                'busy': True,
                'current_version': state.get('current_version') or get_app_version_func(),
                'latest_version': state.get('latest_version') or '',
                'asset_name': state.get('asset_name') or '',
                'source': state.get('source') or '',
                'message': state.get('message') or 'Software update is already in progress.',
                'install_url': url_for('sw_update_install'),
            })

        current_version = get_app_version_func()
        identity = _identity_snapshot()
        try:
            result = _check_for_update(current_version, identity)
        except Exception as exc:
            return jsonify({
                'ok': False,
                'update_available': False,
                'error': f'Unable to reach software update source: {exc}',
            }), 502

        return jsonify({
            'ok': True,
            'update_available': bool(result.get('update_available')),
            'current_version': result.get('current_version') or current_version,
            'latest_version': result.get('latest_version') or current_version,
            'asset_name': result.get('asset_name') or '',
            'source': result.get('source') or '',
            'message': (
                f"A new software update is available: {result.get('latest_version')}"
                if result.get('update_available')
                else f"Stdytime is already up to date at version {current_version}."
            ),
            'install_url': url_for('sw_update_install'),
            'repo_url': result.get('repo_url') or '',
            'release_notes': _normalize_release_notes(result.get('release_notes') or ''),
        })

    @app.route('/api/sw-update/status', methods=['GET'])
    @require_login
    def sw_update_status_api():
        return jsonify({'ok': True, **_get_update_state()})

    @app.route('/api/sw-update/confirm-install', methods=['POST'])
    @require_login
    def sw_update_confirm_install():
        state = _get_update_state()
        if state.get('status') != 'ready_for_manual_install':
            return jsonify({'ok': False, 'error': 'No pending install to confirm.'}), 409
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload if isinstance(payload, dict) else {}
        _set_browser_confirmed(str(payload.get('client_id') or ''))
        _CONFIRM_INSTALL_EVENT.set()
        return jsonify({'ok': True})

    @app.route('/api/sw-update/heartbeat', methods=['POST'])
    @require_login
    def sw_update_heartbeat_api():
        payload = request.get_json(silent=True) if request.is_json else {}
        payload = payload if isinstance(payload, dict) else {}
        client_id = str(payload.get('client_id') or '').strip()
        _register_browser_heartbeat(client_id)
        if bool(payload.get('closed')):
            _register_browser_close_signal(client_id)
        return jsonify({'ok': True, 'ts': time.time()})

    @app.route('/api/sw-update/browser-closed', methods=['POST'])
    @require_login
    def sw_update_browser_closed_api():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
            try:
                raw_body = request.get_data(cache=False, as_text=True) or ''
                if raw_body.strip().startswith('{'):
                    parsed = json.loads(raw_body)
                    if isinstance(parsed, dict):
                        payload = parsed
            except Exception:
                payload = {}

        client_id = str(payload.get('client_id') or '').strip()
        _register_browser_close_signal(client_id)
        return ('', 204)

    @app.route('/api/sw-update/diagnostics', methods=['GET'])
    @require_admin
    def sw_update_diagnostics_api():
        state = _get_update_state()
        return jsonify({
            'ok': True,
            'state': state,
            'handoff': _read_handoff_state(),
            'debug_log_tail': _read_debug_log_tail(),
            'debug_log_path': str(_sw_update_debug_log_path()),
            'handoff_path': str(_sw_update_handoff_path()),
        })

    @app.route('/sw-update/install', methods=['GET'])
    @require_login
    def sw_update_install():
        state = _get_update_state()
        if state.get('status') not in _ACTIVE_UPDATE_STATUSES:
            current_version = get_app_version_func()
            identity = _identity_snapshot()
            _set_update_state(
                status='checking',
                message='Checking for a newer software update and preparing download ticket...',
                error='',
                current_version=current_version,
                latest_version='',
                asset_name='',
                repo_url=_gateway_base_url() or _repo_url(),
                source='gateway' if _gateway_enabled() else 'github-fallback',
                debug_log_path=str(_sw_update_debug_log_path()),
                download_dir='',
                installer_name='',
                installer_path='',
                release_notes='',
                started_at=time.time(),
            )
            worker = threading.Thread(
                target=_background_install_worker,
                args=(identity,),
                daemon=True,
                name='sw-update-worker',
            )
            _CONFIRM_INSTALL_EVENT.clear()
            _reset_browser_monitor_state()
            worker.start()

        return render_template('sw_update_installing.html')
