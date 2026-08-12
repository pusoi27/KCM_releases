"""Publish release assets to a GitHub Release.

Usage (from project root):
    .venv/Scripts/python.exe scripts/publish_github_release_assets.py \
        --repo pusoi27/stdytime_releases \
        --tag v01.03.184 \
        --title "Stdytime 01.03.184" \
        --asset stdytime_installer_v01_03_184.zip \
        --asset stdytime_installer_v01_03_184.zip.sha256
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


GITHUB_API = "https://api.github.com"


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _resolve_token(explicit_token: str | None, root: Path) -> str:
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()

    env_publish_token = (os.getenv("SW_UPDATE_GITHUB_PUBLISH_TOKEN") or "").strip()
    if env_publish_token:
        return env_publish_token

    env_token = (os.getenv("SW_UPDATE_GITHUB_TOKEN") or "").strip()
    if env_token:
        return env_token

    dotenv = _read_dotenv(root / ".env")
    publish_token = (dotenv.get("SW_UPDATE_GITHUB_PUBLISH_TOKEN") or "").strip()
    if publish_token:
        return publish_token

    token = (dotenv.get("SW_UPDATE_GITHUB_TOKEN") or "").strip()
    if token:
        return token

    raise RuntimeError(
        "Missing GitHub token. Set SW_UPDATE_GITHUB_PUBLISH_TOKEN (preferred) or "
        "SW_UPDATE_GITHUB_TOKEN in environment/.env, "
        "or pass --token explicitly."
    )


def _github_request(
    method: str,
    url: str,
    *,
    token: str,
    timeout: int = 45,
    expected: tuple[int, ...] = (200,),
    **kwargs: Any,
) -> requests.Response:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Stdytime-Release-Publisher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    provided = kwargs.pop("headers", None) or {}
    headers.update({str(k): str(v) for k, v in dict(provided).items()})

    response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if response.status_code not in expected:
        body = (response.text or "").strip()
        if response.status_code == 403 and "Resource not accessible by personal access token" in body:
            raise RuntimeError(
                "GitHub token lacks permission to manage releases. "
                "Use SW_UPDATE_GITHUB_PUBLISH_TOKEN (or --token) with access to this repo and "
                "fine-grained permission 'Contents: Read and write' (or classic 'repo' scope)."
            )
        raise RuntimeError(f"GitHub API {method} {url} failed ({response.status_code}): {body[:600]}")
    return response


def _get_or_create_release(
    repo: str,
    tag: str,
    *,
    title: str,
    body: str,
    token: str,
) -> dict[str, Any]:
    by_tag = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    response = requests.get(
        by_tag,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "Stdytime-Release-Publisher",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=45,
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code != 404:
        body_text = (response.text or "").strip()
        raise RuntimeError(f"GitHub API GET {by_tag} failed ({response.status_code}): {body_text[:600]}")

    create_url = f"{GITHUB_API}/repos/{repo}/releases"
    payload = {
        "tag_name": tag,
        "name": title,
        "body": body,
        "draft": False,
        "prerelease": False,
        "generate_release_notes": False,
    }
    created = _github_request(
        "POST",
        create_url,
        token=token,
        json=payload,
        expected=(201,),
    )
    return created.json()


def _delete_existing_asset_if_any(repo: str, release: dict[str, Any], asset_name: str, *, token: str) -> None:
    assets = release.get("assets") if isinstance(release, dict) else []
    if not isinstance(assets, list):
        return

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("name") or "").strip().lower() != asset_name.strip().lower():
            continue
        asset_id = asset.get("id")
        if not asset_id:
            continue
        delete_url = f"{GITHUB_API}/repos/{repo}/releases/assets/{asset_id}"
        _github_request("DELETE", delete_url, token=token, expected=(204,))


def _upload_asset(repo: str, release: dict[str, Any], asset_path: Path, *, token: str) -> str:
    upload_url_template = str(release.get("upload_url") or "").strip()
    if not upload_url_template:
        raise RuntimeError("Release payload did not include upload_url.")

    upload_url_base = upload_url_template.split("{", 1)[0]
    upload_url = f"{upload_url_base}?{urlencode({'name': asset_path.name})}"

    upload_retries_raw = (os.getenv("SW_UPDATE_GITHUB_UPLOAD_RETRIES") or "3").strip()
    try:
        upload_retries = max(1, min(8, int(upload_retries_raw)))
    except ValueError:
        upload_retries = 3

    upload_timeout_raw = (os.getenv("SW_UPDATE_GITHUB_UPLOAD_TIMEOUT_SECONDS") or "1800").strip()
    try:
        upload_timeout_seconds = max(300, min(7200, int(upload_timeout_raw)))
    except ValueError:
        upload_timeout_seconds = 1800

    last_error: Exception | None = None
    for attempt in range(1, upload_retries + 1):
        try:
            with asset_path.open("rb") as handle:
                response = _github_request(
                    "POST",
                    upload_url,
                    token=token,
                    headers={
                        "Content-Type": "application/octet-stream",
                    },
                    data=handle,
                    expected=(201,),
                    timeout=(30, upload_timeout_seconds),
                )
            payload = response.json()
            return str(payload.get("browser_download_url") or "").strip()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as exc:
            last_error = exc
            if attempt >= upload_retries:
                break
            wait_seconds = min(30, 2 * attempt)
            print(
                f"Upload attempt {attempt}/{upload_retries} for {asset_path.name} failed ({exc}). "
                f"Retrying in {wait_seconds}s...",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    if last_error:
        raise RuntimeError(
            f"Upload failed for {asset_path.name} after {upload_retries} attempts: {last_error}"
        )

    raise RuntimeError(f"Upload failed for {asset_path.name} due to an unknown error.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish local files as GitHub Release assets.")
    parser.add_argument("--repo", required=True, help="GitHub repo slug, e.g. owner/repo")
    parser.add_argument("--tag", required=True, help="Release tag, e.g. v01.03.184")
    parser.add_argument("--title", required=True, help="Release title")
    parser.add_argument("--body", default="Automated release assets for Stdytime updater.", help="Release body text")
    parser.add_argument("--token", default=None, help="GitHub token (optional; env/.env fallback supported)")
    parser.add_argument("--asset", action="append", required=True, help="Asset file path (repeatable)")
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    root = Path.cwd()
    token = _resolve_token(args.token, root)

    assets = [Path(item).resolve() for item in args.asset]
    missing = [str(path) for path in assets if not path.exists()]
    if missing:
        raise RuntimeError(f"Missing asset file(s): {', '.join(missing)}")

    release = _get_or_create_release(
        args.repo.strip(),
        args.tag.strip(),
        title=args.title.strip(),
        body=args.body,
        token=token,
    )

    # Refresh release each iteration so asset delete checks remain accurate after uploads.
    for asset_path in assets:
        release_id = release.get("id")
        if not release_id:
            raise RuntimeError("Release payload missing id.")
        release_url = f"{GITHUB_API}/repos/{args.repo.strip()}/releases/{release_id}"
        release = _github_request("GET", release_url, token=token, expected=(200,)).json()
        _delete_existing_asset_if_any(args.repo.strip(), release, asset_path.name, token=token)
        url = _upload_asset(args.repo.strip(), release, asset_path, token=token)
        print(f"Uploaded: {asset_path.name}")
        print(f"URL: {url}")

    print(json.dumps({
        "ok": True,
        "repo": args.repo.strip(),
        "tag": args.tag.strip(),
        "release_url": str(release.get("html_url") or ""),
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
