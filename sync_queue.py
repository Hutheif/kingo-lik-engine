# sync_queue.py
"""
Sync Queue — stores translations locally when cloud is unavailable,
pushes them to cloud when internet recovers.
"""
import os, json, time, threading, requests
from datetime import datetime

SYNC_FILE = "pending_sync.json"
PING_URL  = "https://www.google.com"
SYNC_INTERVAL = 60  # check every 60 seconds

def _load_queue() -> list:
    if not os.path.exists(SYNC_FILE):
        return []
    with open(SYNC_FILE) as f:
        try:
            return json.load(f)
        except:
            return []

def _save_queue(queue: list):
    with open(SYNC_FILE, "w") as f:
        json.dump(queue, f, indent=2)

def add_to_sync_queue(session_id: str, result: dict):
    """Call this when a local engine processes a result that needs cloud backup."""
    queue = _load_queue()
    queue.append({
        "session_id": session_id,
        "result":     result,
        "queued_at":  datetime.utcnow().isoformat(),
        "attempts":   0
    })
    _save_queue(queue)
    print(f"[SYNC] Added {session_id} to sync queue ({len(queue)} pending)")

def is_internet_available(timeout: int = 3) -> bool:
    """Fast ping test — returns True only if cloud responds within timeout."""
    try:
        requests.get(PING_URL, timeout=timeout)
        return True
    except:
        return False

def process_sync_queue():
    """
    Background loop — runs every 60s.
    When internet is available, re-processes queued local results
    through Gemini for higher quality translation.
    """
    while True:
        time.sleep(SYNC_INTERVAL)
        queue = _load_queue()

        if not queue:
            continue

        if not is_internet_available():
            print(f"[SYNC] Internet not available — {len(queue)} items waiting")
            continue

        print(f"[SYNC] Internet restored — processing {len(queue)} queued items")
        remaining = []

        for item in queue:
            session_id = item["session_id"]
            try:
                from database import _load, _write
                sessions = _load()
                if session_id not in sessions:
                    continue

                session = sessions[session_id]
                recording_url = session.get("recording_url")

                if recording_url:
                    from translator import process_recording
                    result = process_recording(session_id, recording_url)
                    print(f"[SYNC] Upgraded {session_id} from local to cloud")
                else:
                    print(f"[SYNC] No recording URL for {session_id} — skipping")

            except Exception as e:
                item["attempts"] += 1
                if item["attempts"] < 3:
                    remaining.append(item)
                    print(f"[SYNC] Failed {session_id} (attempt {item['attempts']}): {e}")
                else:
                    print(f"[SYNC] Giving up on {session_id} after 3 attempts")

        _save_queue(remaining)

def start_sync_worker():
    """Start the sync queue in a background daemon thread."""
    thread = threading.Thread(target=process_sync_queue)
    thread.daemon = True
    thread.start()
    print(f"[SYNC] Background sync worker started — checks every {SYNC_INTERVAL}s")