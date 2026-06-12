"""
Version bump utility for Stdytime.

Increments the patch segment of the VERSION file (xx.xx.xx -> xx.xx.(xx+1)),
preserving zero-padding width of each segment.

Also appends an entry to RELEASE_NOTES.md so every version bump has
corresponding release notes.

Usage:
    python scripts/version_bump.py [optional_path_to_VERSION] [--note "summary"]
"""
from __future__ import annotations
import argparse
from datetime import datetime
import os
import re
import sys

DEFAULT_VERSION = "00.00.01"

def find_version_path(arg_path: str | None = None) -> str:
    if arg_path:
        return os.path.abspath(arg_path)
    # VERSION is at project root (parent of scripts/)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(root, "VERSION")


def find_release_notes_path(version_path: str) -> str:
    root = os.path.dirname(os.path.abspath(version_path))
    return os.path.join(root, "RELEASE_NOTES.md")


def read_version(vpath: str) -> tuple[str, list[str]]:
    if not os.path.exists(vpath):
        return DEFAULT_VERSION, DEFAULT_VERSION.split('.')
    try:
        with open(vpath, 'r', encoding='utf-8') as f:
            raw = (f.read().strip() or DEFAULT_VERSION)
    except Exception:
        return DEFAULT_VERSION, DEFAULT_VERSION.split('.')

    # Expect pattern like 06.07.08 (digits with dots)
    if not re.match(r"^\d+\.\d+\.\d+$", raw):
        return DEFAULT_VERSION, DEFAULT_VERSION.split('.')
    return raw, raw.split('.')


def bump_patch(parts: list[str]) -> list[str]:
    # Preserve zero-padding widths per segment
    widths = [len(p) for p in parts]
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) + 1
    except ValueError:
        major, minor, patch = 0, 0, 1

    return [
        str(major).zfill(widths[0]),
        str(minor).zfill(widths[1]),
        str(patch).zfill(widths[2]),
    ]


def write_version(vpath: str, new_version: str) -> None:
    with open(vpath, 'w', encoding='utf-8') as f:
        f.write(new_version)


def ensure_release_notes_file(notes_path: str) -> None:
    if os.path.exists(notes_path):
        return
    with open(notes_path, 'w', encoding='utf-8') as f:
        f.write("# Release Notes\n\n")


def append_release_note(notes_path: str, version_raw: str, note: str | None = None) -> None:
    ensure_release_notes_file(notes_path)
    today = datetime.now().strftime('%Y-%m-%d')
    clean_note = (note or '').strip() or 'TODO: summarize changes for this release.'
    entry = (
        f"## v{version_raw} - {today}\n"
        f"- {clean_note}\n\n"
    )
    with open(notes_path, 'a', encoding='utf-8') as f:
        f.write(entry)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Bump VERSION patch and append release notes.')
    parser.add_argument('version_path', nargs='?', default=None, help='Optional path to VERSION file')
    parser.add_argument('--note', default=None, help='Release note summary for this version bump')
    return parser.parse_args(argv[1:])


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    vpath = find_version_path(args.version_path)
    old_raw, parts = read_version(vpath)
    new_parts = bump_patch(parts)
    new_raw = '.'.join(new_parts)
    write_version(vpath, new_raw)
    notes_path = find_release_notes_path(vpath)
    append_release_note(notes_path, new_raw, args.note)
    print(
        f"Version bumped: {old_raw} -> {new_raw}\n"
        f"Path: {vpath}\n"
        f"Release notes updated: {notes_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
