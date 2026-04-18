"""
fix_db.py — Run this ONCE to fix the database schema.

Usage:
    python fix_db.py

This adds the missing columns to your existing kingolik.db without
losing any data. Safe to run multiple times.
"""
import sqlite3, os

DB_PATH = "kingolik.db"

if not os.path.exists(DB_PATH):
    print(f"ERROR: {DB_PATH} not found. Run from your King'olik project folder.")
    exit(1)

con = sqlite3.connect(DB_PATH)
con.execute("PRAGMA journal_mode=WAL")

columns_to_add = [
    ("correction",    "TEXT DEFAULT ''"),
    ("audio_url",     "TEXT DEFAULT ''"),
    ("notes",         "TEXT DEFAULT ''"),
    ("note",          "TEXT DEFAULT ''"),
    ("recording_url", "TEXT DEFAULT ''"),
    ("duration",      "TEXT DEFAULT ''"),
    ("source_wav",    "TEXT DEFAULT ''"),
    ("handled",       "INTEGER DEFAULT 0"),
    ("created_at",    "TEXT DEFAULT (datetime('now'))"),
]

print(f"Fixing schema in {DB_PATH}...")
for col_name, col_def in columns_to_add:
    try:
        con.execute(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}")
        con.commit()
        print(f"  ✓ Added column: {col_name}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print(f"  · Already exists: {col_name}")
        else:
            print(f"  ✗ Error on {col_name}: {e}")

# Verify correction column works
try:
    count = con.execute(
        "SELECT COUNT(*) FROM sessions WHERE correction IS NOT NULL AND TRIM(correction) != ''"
    ).fetchone()[0]
    print(f"\n✓ Schema OK. Current gold pairs in DB: {count}")
except Exception as e:
    print(f"\n✗ Verification failed: {e}")

# Show current columns
cols = [row[1] for row in con.execute("PRAGMA table_info(sessions)").fetchall()]
print(f"\nAll columns in sessions table:\n  {', '.join(cols)}")

con.close()
print("\nDone. Restart your Flask server now.")