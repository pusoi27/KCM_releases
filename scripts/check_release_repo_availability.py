from __future__ import annotations

import json
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
        os.getenv("SW_UPDATE_GITHUB_PUBLISH_TOKEN", "").strip()
        or os.getenv("SW_UPDATE_GITHUB_TOKEN", "").strip()
        or env_file.get("SW_UPDATE_GITHUB_PUBLISH_TOKEN", "").strip()
        or env_file.get("SW_UPDATE_GITHUB_TOKEN", "").strip()
    )

    url = "https://api.github.com/repos/pusoi27/stdytime_releases/releases"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Stdytime-Release-Inspector",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(url, headers=headers, timeout=45)
    print(f"status={response.status_code}")

    if response.status_code != 200:
        body = (response.text or "").strip()
        print(body[:1200])
        return 0

    payload = response.json()
    releases = payload if isinstance(payload, list) else []
    print(f"release_count={len(releases)}")

    for rel in releases:
        tag = str(rel.get("tag_name") or "")
        name = str(rel.get("name") or "")
        published_at = str(rel.get("published_at") or "")
        draft = bool(rel.get("draft"))
        prerelease = bool(rel.get("prerelease"))
        assets = rel.get("assets") if isinstance(rel.get("assets"), list) else []
        asset_names = [str(asset.get("name") or "") for asset in assets if isinstance(asset, dict)]

        print(
            json.dumps(
                {
                    "tag": tag,
                    "name": name,
                    "published_at": published_at,
                    "draft": draft,
                    "prerelease": prerelease,
                    "asset_count": len(asset_names),
                    "assets": asset_names,
                },
                ensure_ascii=False,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
