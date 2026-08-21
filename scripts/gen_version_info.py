"""
Generates version_info.txt (PyInstaller Windows version resource) from the VERSION file.

This embeds CompanyName/ProductName/FileDescription etc. into Stdytime.exe so Windows
Explorer (Properties > Details tab, and the "Publisher" list column) shows populated
metadata instead of blank fields. Run before PyInstaller builds.

Usage:
    python scripts/gen_version_info.py [optional_path_to_VERSION] [optional_output_path]
"""
from __future__ import annotations
import os
import re
import sys

COMPANY_NAME = "Adocta Tech LLC"
PRODUCT_NAME = "Stdytime"
FILE_DESCRIPTION = "Stdytime Study Time Tracker"
INTERNAL_NAME = "Stdytime"
ORIGINAL_FILENAME = "Stdytime.exe"
LEGAL_COPYRIGHT = "Copyright (C) Adocta Tech LLC"

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'{company}'),
        StringStruct(u'FileDescription', u'{description}'),
        StringStruct(u'FileVersion', u'{version_str}'),
        StringStruct(u'InternalName', u'{internal_name}'),
        StringStruct(u'LegalCopyright', u'{copyright}'),
        StringStruct(u'OriginalFilename', u'{original_filename}'),
        StringStruct(u'ProductName', u'{product_name}'),
        StringStruct(u'ProductVersion', u'{version_str}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""


def find_version_path(arg_path: str | None = None) -> str:
    if arg_path:
        return os.path.abspath(arg_path)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(root, "VERSION")


def find_output_path(arg_path: str | None = None) -> str:
    if arg_path:
        return os.path.abspath(arg_path)
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return os.path.join(root, "version_info.txt")


def read_version_parts(vpath: str) -> tuple[int, int, int]:
    raw = "0.0.0"
    if os.path.exists(vpath):
        with open(vpath, "r", encoding="utf-8") as f:
            raw = (f.read().strip() or raw)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", raw)
    if not match:
        return 0, 0, 0
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def main(argv: list[str]) -> int:
    vpath = find_version_path(argv[1] if len(argv) > 1 else None)
    out_path = find_output_path(argv[2] if len(argv) > 2 else None)
    major, minor, patch = read_version_parts(vpath)
    version_str = f"{major}.{minor}.{patch}"
    content = TEMPLATE.format(
        major=major,
        minor=minor,
        patch=patch,
        version_str=version_str,
        company=COMPANY_NAME,
        description=FILE_DESCRIPTION,
        internal_name=INTERNAL_NAME,
        copyright=LEGAL_COPYRIGHT,
        original_filename=ORIGINAL_FILENAME,
        product_name=PRODUCT_NAME,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Wrote version resource: {out_path} (version {version_str})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
