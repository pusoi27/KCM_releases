from __future__ import annotations

import os
from pathlib import Path

import requests


def read_dotenv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def main() -> int:
    env_file = read_dotenv(Path(".env"))
    token = (
        os.getenv("SW_UPDATE_GITHUB_TOKEN", "").strip()
        or env_file.get("SW_UPDATE_GITHUB_TOKEN", "").strip()
    )
    if not token:
        print("No SW_UPDATE_GITHUB_TOKEN found; cannot probe private release asset API access.")
        return 0

    headers_json = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Stdytime-SW-Asset-Probe",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    releases_url = "https://api.github.com/repos/pusoi27/stdytime_releases/releases?per_page=10"
    r = requests.get(releases_url, headers=headers_json, timeout=45)
    print(f"releases_status={r.status_code}")
    if r.status_code != 200:
        print((r.text or "")[:800])
        return 0

    releases = r.json() if isinstance(r.json(), list) else []
    if not releases:
        print("No releases returned.")
        return 0

    target_asset = None
    for rel in releases:
        assets = rel.get("assets") if isinstance(rel.get("assets"), list) else []
        for asset in assets:
            name = str(asset.get("name") or "")
            if name.endswith(".zip"):
                target_asset = asset
                break
        if target_asset:
            break

    if not target_asset:
        print("No .zip release asset found to probe.")
        return 0

    asset_name = str(target_asset.get("name") or "")
    asset_api_url = str(target_asset.get("url") or "")
    browser_url = str(target_asset.get("browser_download_url") or "")
    print(f"asset_name={asset_name}")

    headers_bin = {
        "Accept": "application/octet-stream",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Stdytime-SW-Asset-Probe",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    api_probe = requests.get(asset_api_url, headers=headers_bin, timeout=45, allow_redirects=False)
    print(f"asset_api_status={api_probe.status_code}")
    if api_probe.status_code in (301, 302, 303, 307, 308):
        print(f"asset_api_location={api_probe.headers.get('Location','')[:200]}")

    browser_probe = requests.get(browser_url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "Stdytime-SW-Asset-Probe",
    }, timeout=45, allow_redirects=False)
    print(f"browser_url_status={browser_probe.status_code}")
    if browser_probe.status_code in (301, 302, 303, 307, 308):
        print(f"browser_url_location={browser_probe.headers.get('Location','')[:200]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
