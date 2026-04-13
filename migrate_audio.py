# migrate_audio.py — run once to fix old sessions
import json, os, shutil

DB_FILE     = "sessions.json"
RECORDINGS  = "recordings"

with open(DB_FILE) as f:
    sessions = json.load(f)

# Find the newest source audio file
source_files = [
    f for f in os.listdir(RECORDINGS)
    if f.endswith(".wav")
    and "_clean" not in f
    and not f.startswith("ATUid_")
    and not f.startswith("local_test_")
]
source_files.sort(key=lambda f: os.path.getmtime(
    os.path.join(RECORDINGS, f)), reverse=True)

if not source_files:
    print("No source WAV files found")
    exit()

newest_clean = source_files[0].replace(".wav", "_clean.wav")
newest_clean_path = os.path.join(RECORDINGS, newest_clean)

if not os.path.exists(newest_clean_path):
    print(f"Clean version not found: {newest_clean}")
    exit()

print(f"Using source: {newest_clean}")

fixed = 0
for session_id, session in sessions.items():
    copy_path = os.path.join(RECORDINGS, f"{session_id}_raw_clean.wav")
    if not os.path.exists(copy_path):
        shutil.copy2(newest_clean_path, copy_path)
        print(f"Fixed: {session_id[-8:]}")
        fixed += 1

print(f"\nMigrated {fixed} sessions")