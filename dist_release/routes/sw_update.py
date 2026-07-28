from __future__ import annotations

from flask import jsonify, render_template, url_for
import hashlib
import hmac
import os
import re
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
from routes.auth import require_login


_RELEASE_ASSET_PATTERN = re.compile(r'^stdytime_installer_v(?P<safe>\d+(?:_\d+)*)\.zip$', re.IGNORECASE)
_SHA256_HEX_PATTERN = re.compile(r'^[0-9a-fA-F]{64}$')
_UPDATE_LOCK = threading.Lock()
_UPDATE_STATE = {
    'status': 'idle',
    'message': '',
    'error': '',
    'current_version': '',
    'latest_version': '',
    'asset_name': '',
    'repo_url': '',
    'source': '',
    'started_at': 0.0,
    'updated_at': 0.0,
}


def _set_update_state(**fields) -> dict:
    with _UPDATE_LOCK:
        _UPDATE_STATE.update(fields)
        _UPDATE_STATE['updated_at'] = time.time()
        return dict(_UPDATE_STATE)


def _get_update_state() -> dict:
    with _UPDATE_LOCK:
        return dict(_UPDATE_STATE)


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
        raise RuntimeError('SW_UPDATE_GATEWAY_URL is not configured.')

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
        raise RuntimeError('Gateway returned an invalid check payload.')
    if payload.get('ok') is False:
        raise RuntimeError(str(payload.get('error') or 'Gateway denied update check.'))

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
        raise RuntimeError('Gateway returned an invalid ticket payload.')
    if ticket.get('ok') is False:
        raise RuntimeError(str(ticket.get('error') or 'Gateway denied download ticket request.'))

    download_url = _normalize_gateway_download_url(str(ticket.get('download_url') or ''))
    if not download_url:
        raise RuntimeError('Gateway ticket response did not include download_url.')

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
    assets: list[dict] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if bool(release.get('draft')):
            continue
        if _update_channel() == 'stable' and bool(release.get('prerelease')):
            continue

        release_assets = release.get('assets') if isinstance(release.get('assets'), list) else []
        download_by_name: dict[str, str] = {}
        for item in release_assets:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get('name') or '').strip()
            browser_download_url = str(item.get('browser_download_url') or '').strip()
            if item_name and browser_download_url:
                download_by_name[item_name.lower()] = browser_download_url

        for item in release_assets:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get('name') or '').strip()
            browser_download_url = str(item.get('browser_download_url') or '').strip()
            if not item_name or not browser_download_url:
                continue
            asset = _build_asset_record(item_name, browser_download_url, repo_url)
            if not asset:
                continue

            checksum_name = f"{asset['name']}.sha256"
            signature_name = f"{asset['name']}.minisig"
            asset['checksum_url'] = str(download_by_name.get(checksum_name.lower()) or '').strip()
            asset['signature_url'] = str(download_by_name.get(signature_name.lower()) or '').strip()
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
        'raw': latest_asset,
    }


def _check_for_update(current_version: str, identity: dict) -> dict:
    if _gateway_enabled():
        return _gateway_check_for_update(current_version, identity)

    if _allow_direct_github_fallback():
        return _legacy_check_for_update(current_version)

    raise RuntimeError(
        'Software update source is not configured. '
        'Set SW_UPDATE_REPO_URL for direct mode or SW_UPDATE_GATEWAY_URL for gateway mode.'
    )


def _resolve_install_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent

    local_appdata = os.getenv('LOCALAPPDATA', '').strip()
    if local_appdata:
        return Path(local_appdata) / 'Stdytime'

    return Path(__file__).resolve().parents[1]


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
        raise RuntimeError('SW_UPDATE_MINISIGN_PUBLIC_KEY is required for minisign verification.')

    signature_payload = _normalize_signature_text(signature_text)
    if not signature_payload and signature_url:
        sig_response = requests.get(
            signature_url,
            headers=headers or {'User-Agent': 'Stdytime-SW-Updater'},
            timeout=_gateway_timeout_seconds(),
        )
        if sig_response.status_code in {401, 403}:
            raise RuntimeError('Signature file access was denied by the update source.')
        if sig_response.status_code == 404:
            raise RuntimeError('Signature file was not found for the update payload.')
        sig_response.raise_for_status()
        signature_payload = _normalize_signature_text(sig_response.text)

    if not signature_payload:
        raise RuntimeError('No minisign signature payload is available for this update.')

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
        raise RuntimeError(
            f"minisign executable '{_minisign_bin()}' was not found. Install minisign or set SW_UPDATE_MINISIGN_BIN."
        ) from exc

    if verify.returncode != 0:
        details = (verify.stderr or verify.stdout or '').strip()
        raise RuntimeError(f'Minisign verification failed. {details}')


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
        raise RuntimeError('Update payload URL is missing.')

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
    if response.status_code in {401, 403}:
        raise RuntimeError('Update download was denied by the update server.')
    if response.status_code == 404:
        raise RuntimeError('Update payload is unavailable (404).')
    response.raise_for_status()

    temp_dir = Path(tempfile.mkdtemp(prefix='stdytime_sw_update_'))
    zip_path = temp_dir / (asset_name or 'stdytime_update.zip')
    digest = hashlib.sha256()
    with zip_path.open('wb') as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if chunk:
                handle.write(chunk)
                digest.update(chunk)

    inline_expected_hash = _normalize_sha256_text(expected_sha256)
    checksum_expected_hash = ''
    if checksum_url:
        checksum_response = requests.get(
            checksum_url,
            headers=headers,
            timeout=_gateway_timeout_seconds(),
        )
        if checksum_response.status_code in {401, 403}:
            raise RuntimeError('Checksum file access was denied by the update source.')
        if checksum_response.status_code == 404:
            raise RuntimeError('Checksum file was not found for the update payload.')
        checksum_response.raise_for_status()
        checksum_expected_hash = _extract_sha256_from_text(checksum_response.text)

    # Prefer checksum sidecar hash when available to avoid stale inline hashes from
    # intermediate update metadata caches.
    resolved_expected_hash = checksum_expected_hash or inline_expected_hash

    if _require_checksum_verification() and not resolved_expected_hash:
        raise RuntimeError('Checksum verification required, but no valid SHA-256 value is available for this update.')

    if resolved_expected_hash:
        downloaded_hash = digest.hexdigest().lower()
        if downloaded_hash != resolved_expected_hash:
            raise RuntimeError(
                f'Checksum verification failed for {asset_name}: expected {resolved_expected_hash}, got {downloaded_hash}.'
            )

    signature_payload = _normalize_signature_text(expected_signature)
    signature_source_url = str(signature_url or '').strip()
    public_key = str(minisign_public_key or _minisign_public_key() or '').strip()

    if _require_signature_verification() and not (signature_payload or signature_source_url):
        raise RuntimeError('Signature verification is required, but no minisign signature is available for this update.')

    should_verify_signature = bool(signature_payload or signature_source_url or _require_signature_verification())
    if should_verify_signature:
        _verify_minisign_signature(
            zip_path,
            signature_text=signature_payload,
            signature_url=signature_source_url,
            headers=headers,
            public_key=public_key,
        )

    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError('Downloaded update is not a valid ZIP archive.')

    extract_dir = temp_dir / 'payload'
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as archive:
        archive.extractall(extract_dir)

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


def _write_update_helper_script(source_dir: Path, install_dir: Path, wait_pid: int) -> Path:
    helper_dir = Path(tempfile.mkdtemp(prefix='stdytime_sw_apply_'))
    helper_path = helper_dir / 'apply_sw_update.ps1'
    script = f"""
param(
    [int]$WaitPid,
    [string]$SourceDir,
    [string]$InstallDir
)

$ErrorActionPreference = 'Stop'

for ($i = 0; $i -lt 240; $i++) {{
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) {{
        break
    }}
    Start-Sleep -Milliseconds 500
}}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$resolvedSource = (Resolve-Path -LiteralPath $SourceDir).Path

Get-ChildItem -LiteralPath $resolvedSource -Recurse -Force | Sort-Object FullName | ForEach-Object {{
    $relative = $_.FullName.Substring($resolvedSource.Length).TrimStart('\\')
    if ([string]::IsNullOrWhiteSpace($relative)) {{
        return
    }}
    if ($relative -ieq 'db_config.json') {{
        return
    }}
    if ($relative -match '(^|\\)data\\backups(\\|$)') {{
        return
    }}
    if (-not $_.PSIsContainer -and $relative -like '*.db') {{
        return
    }}

    $destination = Join-Path $InstallDir $relative
    if ($_.PSIsContainer) {{
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        return
    }}

    $parent = Split-Path -Parent $destination
    if ($parent) {{
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }}
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
}}

$exePath = Join-Path $InstallDir 'Stdytime.exe'
if (Test-Path -LiteralPath $exePath) {{
    Start-Process -FilePath $exePath -WorkingDirectory $InstallDir | Out-Null
    exit 0
}}

$launcherPath = Join-Path $InstallDir 'launcher.py'
if (Test-Path -LiteralPath $launcherPath) {{
    $pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $pythonw) {{
        $pythonw = Get-Command python.exe -ErrorAction SilentlyContinue
    }}
    if ($pythonw) {{
        Start-Process -FilePath $pythonw.Source -ArgumentList 'launcher.py' -WorkingDirectory $InstallDir | Out-Null
        exit 0
    }}
}}

throw 'Update applied, but no launch target was found.'
""".strip()
    helper_path.write_text(script, encoding='utf-8')
    return helper_path


def _launch_update_helper(source_dir: Path, install_dir: Path, wait_pid: int) -> None:
    helper_path = _write_update_helper_script(source_dir, install_dir, wait_pid)
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
        ],
        cwd=str(install_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creation_flags,
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
        }

    raise RuntimeError('No downloadable payload URL is available for this update source.')


def register_sw_update_routes(app, get_app_version_func, shutdown_flush_once=None):
    def _background_install_worker(identity: dict) -> None:
        try:
            current_version = get_app_version_func()
            result = _check_for_update(current_version, identity)

            if not result.get('update_available'):
                _set_update_state(
                    status='idle',
                    message=f'No newer software update found. Current version {current_version} is already up to date.',
                    error='',
                    current_version=current_version,
                    latest_version=result.get('latest_version') or current_version,
                    asset_name='',
                    repo_url=result.get('repo_url') or '',
                    source=result.get('source') or '',
                )
                return

            _set_update_state(
                status='downloading',
                message=f"Downloading {result.get('asset_name') or 'software update'}...",
                error='',
                current_version=current_version,
                latest_version=result.get('latest_version') or '',
                asset_name=result.get('asset_name') or '',
                repo_url=result.get('repo_url') or '',
                source=result.get('source') or '',
            )

            download_spec = _resolve_download_spec(result, identity)
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
                if 'Checksum verification failed' not in str(first_exc) or result.get('source') != 'gateway':
                    raise

                # Transient cache/race hardening: refresh ticket/spec once and retry.
                _set_update_state(
                    status='downloading',
                    message='Checksum mismatch detected. Refreshing download ticket and retrying once...',
                    error='',
                    current_version=current_version,
                    latest_version=result.get('latest_version') or '',
                    asset_name=result.get('asset_name') or '',
                    repo_url=result.get('repo_url') or '',
                    source=result.get('source') or '',
                )

                download_spec = _resolve_download_spec(result, identity)
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

            _set_update_state(
                status='preparing',
                message='Preparing release payload for installation...',
                error='',
                current_version=current_version,
                latest_version=result.get('latest_version') or '',
                asset_name=result.get('asset_name') or '',
                repo_url=result.get('repo_url') or '',
                source=result.get('source') or '',
            )

            payload_root = _detect_payload_root(extract_dir)
            install_dir = _resolve_install_dir()
            install_dir.mkdir(parents=True, exist_ok=True)
            _launch_update_helper(payload_root, install_dir, os.getpid())

            _set_update_state(
                status='restarting',
                message='Applying update and restarting Stdytime...',
                error='',
                current_version=current_version,
                latest_version=result.get('latest_version') or '',
                asset_name=result.get('asset_name') or '',
                repo_url=result.get('repo_url') or '',
                source=result.get('source') or '',
            )

            if callable(shutdown_flush_once):
                try:
                    shutdown_flush_once('software update')
                except Exception:
                    pass

            time.sleep(1.2)
            os._exit(0)
        except Exception as exc:
            _set_update_state(
                status='error',
                message='Software update failed.',
                error=str(exc),
            )

    @app.route('/api/sw-update/check', methods=['GET'])
    @require_login
    def sw_update_check_api():
        state = _get_update_state()
        if state.get('status') in {'checking', 'downloading', 'preparing', 'restarting'}:
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
        })

    @app.route('/api/sw-update/status', methods=['GET'])
    @require_login
    def sw_update_status_api():
        return jsonify({'ok': True, **_get_update_state()})

    @app.route('/sw-update/install', methods=['GET'])
    @require_login
    def sw_update_install():
        state = _get_update_state()
        if state.get('status') not in {'checking', 'downloading', 'preparing', 'restarting'}:
            current_version = get_app_version_func()
            identity = _identity_snapshot()
            _set_update_state(
                status='checking',
                message='Checking for a newer software update...',
                error='',
                current_version=current_version,
                latest_version='',
                asset_name='',
                repo_url=_gateway_base_url() or _repo_url(),
                source='gateway' if _gateway_enabled() else 'github-fallback',
                started_at=time.time(),
            )
            worker = threading.Thread(
                target=_background_install_worker,
                args=(identity,),
                daemon=True,
                name='sw-update-worker',
            )
            worker.start()

        return render_template('sw_update_installing.html')
