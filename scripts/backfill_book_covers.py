"""Backfill missing book cover blobs for existing books.

Usage:
    .venv\\Scripts\\python.exe scripts/backfill_book_covers.py
    .venv\\Scripts\\python.exe scripts/backfill_book_covers.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Iterable


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
        sys.path.insert(0, PROJECT_ROOT)

from modules.database import DB_PATH
from modules.book_manager import (
    _preferred_valid_isbn,
    _fetch_small_cover_blob_from_openlibrary,
)


def _ensure_cover_columns(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cols = [row[1] for row in cur.execute("PRAGMA table_info(books)").fetchall()]
    if "cover_blob" not in cols:
        cur.execute("ALTER TABLE books ADD COLUMN cover_blob BLOB")
    if "cover_mime" not in cols:
        cur.execute("ALTER TABLE books ADD COLUMN cover_mime TEXT DEFAULT ''")
    if "cover_lookup_attempted" not in cols:
        cur.execute("ALTER TABLE books ADD COLUMN cover_lookup_attempted INTEGER DEFAULT 0")
    conn.commit()


def _iter_books_missing_cover(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, isbn, isbn13, cover_blob
        FROM books
                WHERE COALESCE(cover_blob, X'') = X''
                    AND COALESCE(cover_lookup_attempted, 0) = 0
        ORDER BY id ASC
        """
    )
    return cur.fetchall()


def run_backfill(*, dry_run: bool = False, throttle_ms: int = 120) -> dict:
    updated = 0
    scanned = 0
    skipped_no_valid_isbn = 0
    skipped_not_found = 0
    errors = 0

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_cover_columns(conn)
        rows = _iter_books_missing_cover(conn)

        for row in rows:
            scanned += 1
            book_id = int(row["id"])
            title = str(row["title"] or "").strip() or f"Book {book_id}"
            isbn = str(row["isbn"] or "").strip() or None
            isbn13 = str(row["isbn13"] or "").strip() or None

            valid_isbn = _preferred_valid_isbn(isbn, isbn13)
            if not valid_isbn:
                skipped_no_valid_isbn += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE books SET cover_lookup_attempted = 1 WHERE id = ?",
                        (book_id,),
                    )
                    conn.commit()
                continue

            try:
                blob, mime = _fetch_small_cover_blob_from_openlibrary(valid_isbn)
            except Exception as exc:
                errors += 1
                print(f"[error] id={book_id} title={title!r}: {exc}")
                continue

            if not blob:
                skipped_not_found += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE books SET cover_lookup_attempted = 1 WHERE id = ?",
                        (book_id,),
                    )
                    conn.commit()
                continue

            if dry_run:
                updated += 1
                print(f"[dry-run] would update id={book_id} title={title!r} isbn={valid_isbn}")
            else:
                conn.execute(
                    "UPDATE books SET cover_blob = ?, cover_mime = ?, cover_lookup_attempted = 1 WHERE id = ?",
                    (sqlite3.Binary(blob), (mime or "image/jpeg"), book_id),
                )
                conn.commit()
                updated += 1
                print(f"[updated] id={book_id} title={title!r} isbn={valid_isbn}")

            if throttle_ms > 0:
                time.sleep(max(0, throttle_ms) / 1000.0)

    return {
        "scanned": scanned,
        "updated": updated,
        "skipped_no_valid_isbn": skipped_no_valid_isbn,
        "skipped_not_found": skipped_not_found,
        "errors": errors,
        "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill missing book cover blobs.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write DB changes.")
    parser.add_argument(
        "--throttle-ms",
        type=int,
        default=120,
        help="Delay between external requests in milliseconds (default: 120).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_backfill(dry_run=bool(args.dry_run), throttle_ms=int(args.throttle_ms))

    print("\n=== Book Cover Backfill Summary ===")
    print(f"Scanned: {result['scanned']}")
    print(f"Updated: {result['updated']}")
    print(f"Skipped (invalid/no ISBN): {result['skipped_no_valid_isbn']}")
    print(f"Skipped (cover not found): {result['skipped_not_found']}")
    print(f"Errors: {result['errors']}")
    print(f"Mode: {'dry-run' if result['dry_run'] else 'write'}")
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
