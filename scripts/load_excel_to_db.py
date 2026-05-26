"""
Load Levels_By_grade.xlsx and Levels_Index.xlsx into SQLite database
Reads Excel files from OneDrive and creates persistent database tables
"""

import pandas as pd
import sqlite3
from pathlib import Path
import sys
import os

# Paths
EXCEL_DIR = Path(os.getenv("STDYTIME_LEVELS_EXCEL_DIR", r"C:\Users\octav\OneDrive\ODI Learning\Stdytime\data"))
DB_PATH = Path(__file__).parent.parent / "data" / "Stdytime.db"

LEVELS_BY_GRADE_FILE = EXCEL_DIR / "Levels_By_grade.xlsx"
LEVELS_INDEX_FILE = EXCEL_DIR / "Levels_Index.xlsx"


def load_levels_by_grade():
    """Load Levels_By_grade.xlsx into database"""
    print(f"Reading {LEVELS_BY_GRADE_FILE}...")
    
    # Read the Excel file - try different sheet names
    try:
        # Try 'grade_table' sheet first (common in shared levels workbooks)
        df = pd.read_excel(LEVELS_BY_GRADE_FILE, sheet_name='grade_table')
        print(f"  Loaded 'grade_table' sheet with {len(df)} rows")
    except Exception as e:
        print(f"  'grade_table' sheet not found, trying first sheet...")
        # Fallback to first sheet
        df = pd.read_excel(LEVELS_BY_GRADE_FILE, sheet_name=0)
        print(f"  Loaded first sheet with {len(df)} rows")
    
    # Display columns and sample data
    print(f"  Columns: {', '.join(df.columns)}")
    print(f"  First 3 rows:\n{df.head(3)}")
    
    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    
    # Drop existing table if it exists
    conn.execute("DROP TABLE IF EXISTS levels_by_grade")
    
    # Write to database
    df.to_sql('levels_by_grade', conn, if_exists='replace', index=False)
    
    # Verify
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM levels_by_grade")
    count = cursor.fetchone()[0]
    print(f"  ✓ Created 'levels_by_grade' table with {count} rows")
    
    # Show table schema
    cursor.execute("PRAGMA table_info(levels_by_grade)")
    schema = cursor.fetchall()
    print(f"  Table schema:")
    for col in schema:
        print(f"    - {col[1]} ({col[2]})")
    
    conn.close()
    return df


def load_levels_index():
    """Load Levels_Index.xlsx into database"""
    print(f"\nReading {LEVELS_INDEX_FILE}...")
    
    # This file typically has multiple sheets, load all
    excel_file = pd.ExcelFile(LEVELS_INDEX_FILE)
    print(f"  Available sheets: {', '.join(excel_file.sheet_names)}")
    
    conn = sqlite3.connect(DB_PATH)
    
    # Load each sheet into a separate table
    for sheet_name in excel_file.sheet_names:
        print(f"\n  Loading sheet: {sheet_name}")
        df = pd.read_excel(LEVELS_INDEX_FILE, sheet_name=sheet_name)
        print(f"    Rows: {len(df)}, Columns: {', '.join(df.columns)}")
        
        # Create table name from sheet name (sanitize)
        table_name = f"levels_index_{sheet_name.lower().replace(' ', '_').replace('-', '_')}"
        
        # Drop existing table
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        
        # Write to database
        df.to_sql(table_name, conn, if_exists='replace', index=False)
        
        # Verify
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cursor.fetchone()[0]
        print(f"    ✓ Created '{table_name}' table with {count} rows")
        
        # Show first few rows
        print(f"    Sample data:\n{df.head(2)}")
    
    conn.close()


def verify_tables():
    """Verify all tables were created successfully"""
    print("\n" + "="*60)
    print("VERIFICATION: Database Tables")
    print("="*60)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # List all tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = cursor.fetchall()
    
    print(f"\nAll tables in {DB_PATH.name}:")
    for table in tables:
        table_name = table[0]
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        count = cursor.fetchone()[0]
        print(f"  - {table_name}: {count} rows")
    
    # Show detailed info for our new tables
    print("\n" + "-"*60)
    print("Detailed Information:")
    print("-"*60)
    
    for table in tables:
        table_name = table[0]
        if 'levels' in table_name.lower():
            print(f"\n{table_name}:")
            cursor.execute(f"PRAGMA table_info({table_name})")
            schema = cursor.fetchall()
            for col in schema:
                print(f"  - {col[1]} ({col[2]})")
    
    conn.close()
    print("\n✓ Database load complete!")


def main():
    """Main execution"""
    try:
        print("="*60)
        print("Loading Excel Files into SQLite Database")
        print("="*60)
        
        # Load Levels_By_grade.xlsx
        load_levels_by_grade()
        
        # Load Levels_Index.xlsx
        load_levels_index()
        
        # Verify
        verify_tables()
        
        return 0
        
    except FileNotFoundError as e:
        print(f"\n✗ Error: File not found - {e}")
        return 1
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
