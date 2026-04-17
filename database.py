# database.py — King'olik SQLite backend
import sqlite3, json, os, threading
from datetime import datetime

DB_PATH = "kingolik.db"
_lock   = threading.Lock()

SMS_ALERT_NUMBER = os.environ.get("ALERT_PHONE", "")


def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def _migrate():
    """Add missing columns safely — runs on every startup."""
    migrations = [
        "ALTER TABLE sessions ADD COLUMN audio_url TEXT DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN notes TEXT DEFAULT ''",
    ]
    with _conn() as con:
        for sql in migrations:
            try:
                con.execute(sql)
                con.commit()
            except Exception:
                pass  # column already exists


def init_db():
    with _lock, _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                phone         TEXT DEFAULT '',
                menu_choice   TEXT DEFAULT '',
                timestamp     TEXT DEFAULT '',
                status        TEXT DEFAULT 'pending_call',
                source_wav    TEXT DEFAULT '',
                recording_url TEXT DEFAULT '',
                audio_url     TEXT DEFAULT '',
                duration      TEXT DEFAULT '',
                handled       INTEGER DEFAULT 0,
                note          TEXT DEFAULT '',
                notes         TEXT DEFAULT '',
                correction    TEXT DEFAULT '',
                translation   TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        con.commit()
    _migrate()
    print("[DB] SQLite ready →", DB_PATH)


def save_session(data: dict):
    with _lock, _conn() as con:
        con.execute("""
            INSERT INTO sessions
                (session_id, phone, menu_choice, timestamp, status, source_wav)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                phone       = excluded.phone,
                menu_choice = excluded.menu_choice,
                timestamp   = excluded.timestamp,
                status      = excluded.status,
                source_wav  = excluded.source_wav
        """, (
            data["session_id"],
            data.get("phone", ""),
            data.get("menu_choice", ""),
            data.get("timestamp", datetime.utcnow().isoformat()),
            data.get("status", "pending_call"),
            data.get("source_wav", "")
        ))
        con.commit()


def update_call_record(session_id: str, recording_url: str, duration: str):
    with _lock, _conn() as con:
        con.execute("""
            UPDATE sessions
            SET recording_url=?, duration=?, status='recorded'
            WHERE session_id=?
        """, (recording_url, duration or "", session_id))
        con.commit()


def save_audio_url(session_id: str, audio_url: str):
    """Saves the API path for the audio player."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET audio_url=? WHERE session_id=?",
            (audio_url, session_id)
        )
        con.commit()
    print(f"[DB] Audio URL saved → {session_id[-8:]}  url={audio_url}")


def update_call_status(session_id: str, status: str):
    """Updates status field only."""
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET status=? WHERE session_id=?",
            (status, session_id)
        )
        con.commit()


def save_translation(session_id: str, result: dict):
    """
    THE SINGLE AUTHORITATIVE save_translation.

    Accepts result as a DICT (the standard format from translator.py).
    Stores as JSON so dashboard can read result['translation'], result['transcript'] etc.

    DO NOT add another save_translation() below — this is the only one.
    """
    # Guard: skip if already translated to prevent duplicate processing
    current = get_session(session_id)
    if current and current.get("status") in ("translated", "handled"):
        print(f"[DB] Skip duplicate translation for {session_id[-8:]}")
        return

    # Ensure result is a dict
    if not isinstance(result, dict):
        result = {
            "translation": str(result),
            "transcript":  str(result),
            "detected_language": "unknown",
            "engine": "unknown",
            "urgent_keywords": [],
            "confidence": "low"
        }

    translation_json = json.dumps(result, ensure_ascii=False)
    with _lock, _conn() as con:
        con.execute("""
            INSERT INTO sessions (session_id, status, translation, timestamp)
            VALUES (?, 'translated', ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                translation = excluded.translation,
                status      = 'translated'
        """, (session_id, translation_json, datetime.utcnow().isoformat()))
        con.commit()

    print(f"[DB] Translation saved → {session_id[-8:]}  engine={result.get('engine','?')}")

    # WebSocket push to dashboard
    try:
        from app import socketio
        session = get_session(session_id)
        if session:
            socketio.emit("session_updated", session)
            print(f"[WS] Pushed → {session_id[-8:]}")
    except Exception as e:
        print(f"[WS] Push skipped: {e}")

    # Trend engine
    try:
        import importlib.util
        if importlib.util.find_spec("trend_engine"):
            from trend_engine import check_trends, send_trend_sms
            session = get_session(session_id)
            alerts  = check_trends(session or {}, result)
            if alerts:
                send_trend_sms(alerts)
                try:
                    from app import socketio
                    for a in alerts:
                        socketio.emit("trend_alert", a)
                except Exception:
                    pass
    except Exception as e:
        print(f"[TREND] {e}")

    # Webhook
    try:
        from webhook import push_to_ngo_system
        session = get_session(session_id)
        if session:
            push_to_ngo_system(session, result)
    except Exception:
        pass

    # SMS alert on urgent keywords
    keywords = result.get("urgent_keywords") or []
    if keywords and SMS_ALERT_NUMBER:
        threading.Thread(
            target=_send_sms_alert,
            args=(session_id, result, keywords),
            daemon=True
        ).start()


def _send_sms_alert(session_id: str, result: dict, keywords: list):
    try:
        import africastalking
        sms     = africastalking.SMS
        session = get_session(session_id)
        phone   = session.get("phone","unknown") if session else "unknown"
        preview = (result.get("translation") or "")[:100]
        kw_str  = ", ".join(keywords[:5])
        msg     = f"KINGOLIK URGENT\nCaller: {phone}\nAlert: {kw_str}\nSaid: {preview}"
        resp    = sms.send(msg, [SMS_ALERT_NUMBER], sender_id=os.environ.get("SENDER_ID"))
        recips  = resp.get("SMSMessageData",{}).get("Recipients",[])
        status  = recips[0].get("status","?") if recips else "no_recipients"
        print(f"[SMS] Alert sent to {SMS_ALERT_NUMBER}  status={status}")
        if status != "Success":
            print(f"[SMS] Full response: {resp}")
    except Exception as e:
        print(f"[SMS] Alert failed: {e}")


def save_correction(session_id: str, correction: str):
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET correction=? WHERE session_id=?",
            (correction, session_id)
        )
        con.commit()
    print(f"[HITL] Correction saved → {session_id[-8:]}")


def _count_corrections() -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE correction != '' AND correction IS NOT NULL"
        ).fetchone()
    return row[0] if row else 0


def mark_handled(session_id: str):
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET handled=1, status='handled' WHERE session_id=?",
            (session_id,)
        )
        con.commit()
    # Send feedback SMS to caller
    try:
        session = get_session(session_id)
        if session:
            import importlib.util
            if importlib.util.find_spec("trend_engine"):
                from trend_engine import send_feedback_to_caller
                send_feedback_to_caller(session_id, session.get("phone",""), eta_hours=2)
    except Exception:
        pass


def save_note(session_id: str, note: str):
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET note=? WHERE session_id=?",
            (note, session_id)
        )
        con.commit()


def get_all_sessions() -> list:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM sessions ORDER BY timestamp DESC"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d.get("translation"):
            try:
                d["translation"] = json.loads(d["translation"])
            except Exception:
                # If stored as plain string (legacy), wrap it
                raw = d["translation"]
                d["translation"] = {
                    "translation": raw,
                    "transcript":  raw,
                    "detected_language": "sw",
                    "engine": "legacy",
                    "urgent_keywords": [],
                    "confidence": "medium"
                }
        else:
            d["translation"] = {}
        d["handled"] = bool(d.get("handled", 0))
        # Ensure audio_url is set — dashboard uses /api/audio/{session_id}
        if not d.get("audio_url"):
            d["audio_url"] = f"/api/audio/{d['session_id']}"
        result.append(d)
    return result


def get_session(session_id: str):
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("translation"):
        try:
            d["translation"] = json.loads(d["translation"])
        except Exception:
            raw = d["translation"]
            d["translation"] = {
                "translation": raw, "transcript": raw,
                "detected_language": "sw", "engine": "legacy",
                "urgent_keywords": [], "confidence": "medium"
            }
    else:
        d["translation"] = {}
    d["handled"] = bool(d.get("handled", 0))
    if not d.get("audio_url"):
        d["audio_url"] = f"/api/audio/{d['session_id']}"
    return d


def migrate_from_json(json_path: str = "sessions.json"):
    if not os.path.exists(json_path):
        return
    with open(json_path) as f:
        try:
            sessions = json.load(f)
        except Exception:
            return
    count = 0
    for sid, s in sessions.items():
        t = s.get("translation", {})
        with _lock, _conn() as con:
            con.execute("""
                INSERT OR IGNORE INTO sessions
                    (session_id, phone, menu_choice, timestamp, status,
                     source_wav, recording_url, audio_url, duration,
                     handled, note, translation)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sid, s.get("phone",""), s.get("menu_choice",""), s.get("timestamp",""),
                s.get("status","pending_call"), s.get("source_wav",""),
                s.get("recording_url",""), s.get("audio_url",""), s.get("duration",""),
                1 if s.get("handled") else 0, s.get("note",""),
                json.dumps(t) if isinstance(t, dict) else str(t)
            ))
            con.commit()
        count += 1
    if count:
        print(f"[MIGRATE] Imported {count} sessions from {json_path}")


# Legacy shims for compatibility
def _load() -> dict:
    return {s["session_id"]: s for s in get_all_sessions()}


# Initialise on import
init_db()
migrate_from_json()