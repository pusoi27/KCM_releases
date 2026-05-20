import sqlite3
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from modules.database import DB_PATH


LEGACY_GOAL_COLUMNS = (
    "math_goal",
    "math_ws_per_week",
    "math_worksheets_per_week",
    "reading_goal",
    "reading_ws_per_week",
    "reading_worksheets_per_week",
)


def main():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(students)").fetchall()]
        to_drop = [col for col in LEGACY_GOAL_COLUMNS if col in cols]

        if not to_drop:
            print("No legacy goal columns found. Nothing to migrate.")
            return

        for col in to_drop:
            c.execute(f'ALTER TABLE students DROP COLUMN "{col}"')
        conn.commit()

    print(f"Student table migrated. Removed columns: {', '.join(to_drop)}")


if __name__ == "__main__":
    main()
