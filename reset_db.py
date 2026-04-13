# reset_db.py — clears sessions.json for a fresh start
import json, os

DB_FILE = "sessions.json"

if os.path.exists(DB_FILE):
    # Keep a backup first
    import shutil
    shutil.copy(DB_FILE, "sessions_backup.json")
    print(f"Backup saved to sessions_backup.json")

# Write empty database
with open(DB_FILE, "w") as f:
    json.dump({}, f)

print("Database cleared. Dashboard will show 0 messages.")