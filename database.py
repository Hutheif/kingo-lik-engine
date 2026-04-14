# database.py — SQLite backend + SMS urgent alert
import sqlite3, json, os, threading
from datetime import datetime

DB_PATH = "kingolik.db"
_lock   = threading.Lock()

# SMS alert config — caseworker number to notify on urgent keywords
SMS_ALERT_NUMBER = os.environ.get("ALERT_PHONE", "")  # e.g. +254712345678


def _conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


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
                duration      TEXT DEFAULT '',
                handled       INTEGER DEFAULT 0,
                note          TEXT DEFAULT '',
                correction    TEXT DEFAULT '',
                translation   TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        con.commit()
    print("[DB] SQLite ready →", DB_PATH)


def save_session(data: dict):
    with _lock, _conn() as con:
        con.execute("""
            INSERT INTO sessions
                (session_id, phone, menu_choice, timestamp, status, source_wav)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                phone        = excluded.phone,
                menu_choice  = excluded.menu_choice,
                timestamp    = excluded.timestamp,
                status       = excluded.status,
                source_wav   = excluded.source_wav
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


def save_translation(session_id: str, result: dict):
    translation_json = json.dumps(result)
    with _lock, _conn() as con:
        con.execute("""
            INSERT INTO sessions
                (session_id, phone, timestamp, status, translation)
            VALUES (?, 'local-test', datetime('now'), 'translated', ?)
            ON CONFLICT(session_id) DO UPDATE SET
                translation = excluded.translation,
                status      = 'translated'
        """, (session_id, translation_json))
        con.commit()

    engine = result.get("engine", "?")
    print(f"[DB] Translation saved → {session_id[-8:]}  engine={engine}")

    # ── Push to dashboard via WebSocket ──────────────────────
    # Late import avoids circular dependency (database ← app ← database)
    try:
        from app import socketio
        session = get_session(session_id)
        if session:
            socketio.emit("session_updated", session)
            print(f"[WS] Pushed → {session_id[-8:]}")
    except Exception as e:
        print(f"[WS] Push skipped: {e}")

    # ── Trend detection — field pilot logistics engine ───────────
    try:
        import importlib.util, sys
        if importlib.util.find_spec("trend_engine") or "trend_engine" in sys.modules:
            from trend_engine import check_trends, send_trend_sms
        else:
            raise ImportError("trend_engine.py not in project folder")
        session  = get_session(session_id)
        alerts   = check_trends(session or {}, result)
        if alerts:
            send_trend_sms(alerts)
            # Push trend alerts to dashboard via WebSocket
            try:
                from app import socketio
                for alert in alerts:
                    socketio.emit("trend_alert", alert)
            except Exception:
                pass
    except Exception as e:
        print(f"[TREND] Engine error: {e}")

    # ── Push to NGO external system via webhook ──────────────
    try:
        from webhook import push_to_ngo_system
        session = get_session(session_id)
        if session:
            push_to_ngo_system(session, result)
    except Exception as e:
        print(f"[WEBHOOK] Skipped: {e}")

    # ── SMS alert on urgent keywords ──────────────────────────
    keywords = result.get("urgent_keywords") or []
    if keywords and SMS_ALERT_NUMBER:
        threading.Thread(
            target=_send_sms_alert,
            args=(session_id, result, keywords),
            daemon=True
        ).start()


def _send_sms_alert(session_id: str, result: dict, keywords: list):
    """
    Sends an SMS to the caseworker when urgent keywords are detected.
    Sandbox: sender_id=None lets AT use its default (avoids shortcode error).
    Production: set SENDER_ID=KINGOLIK in .env once you have a registered alphanumeric.
    """
    try:
        import africastalking
        sms       = africastalking.SMS
        session   = get_session(session_id)
        phone     = session.get("phone", "unknown") if session else "unknown"
        preview   = (result.get("translation") or "")[:100]
        kw_str    = ", ".join(keywords[:5])
        sender_id = os.environ.get("SENDER_ID") or None

        message = (
            f"KINGOLIK URGENT\n"
            f"Caller: {phone}\n"
            f"Alert: {kw_str}\n"
            f"Said: {preview}"
        )

        response   = sms.send(message, [SMS_ALERT_NUMBER], sender_id=sender_id)
        recipients = response.get("SMSMessageData", {}).get("Recipients", [])

        if recipients:
            status = recipients[0].get("status", "?")
            print(f"[SMS] Alert sent to {SMS_ALERT_NUMBER}  status={status}")
            if status != "Success":
                print(f"[SMS] Full response: {response}")
        else:
            print(f"[SMS] Unexpected response: {response}")

    except Exception as e:
        print(f"[SMS] Alert failed: {e}")
        import traceback
        traceback.print_exc()


def save_correction(session_id: str, correction: str):
    """
    Saves a caseworker's corrected translation.
    Every correction becomes a Gold Standard training pair for HITL.
    """
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET correction=? WHERE session_id=?",
            (correction, session_id)
        )
        con.commit()
    print(f"[HITL] Correction saved → {session_id[-8:]}  "
          f"(gold standard pair #{_count_corrections()})")


def _count_corrections() -> int:
    """Count total gold standard pairs collected."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE correction != ''"
        ).fetchone()
    return row[0] if row else 0


def get_call_count(phone: str, hours: int = 1) -> int:
    """Count sessions from this phone in the last N hours — for rate limiting."""
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE phone=? AND timestamp > ?",
            (phone, cutoff)
        ).fetchone()
    return row[0] if row else 0


def mark_handled(session_id: str):
    with _lock, _conn() as con:
        con.execute(
            "UPDATE sessions SET handled=1, status='handled' WHERE session_id=?",
            (session_id,)
        )
        con.commit()
    # Send "help is on the way" feedback SMS to original caller
    try:
        session = get_session(session_id)
        if session:
            import importlib.util
            if not importlib.util.find_spec("trend_engine"):
                raise ImportError("trend_engine not installed")
            from trend_engine import send_feedback_to_caller
            send_feedback_to_caller(
                session_id=session_id,
                phone=session.get("phone",""),
                eta_hours=2
            )
    except Exception as e:
        print(f"[FEEDBACK] SMS send failed: {e}")


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
                d["translation"] = {}
        else:
            d["translation"] = {}
        d["handled"] = bool(d.get("handled", 0))
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
            d["translation"] = {}
    else:
        d["translation"] = {}
    d["handled"] = bool(d.get("handled", 0))
    return d


def migrate_from_json(json_path: str = "sessions.json"):
    if not os.path.exists(json_path):
        print("[MIGRATE] No sessions.json — skipping")
        return
    with open(json_path) as f:
        try:
            sessions = json.load(f)
        except Exception as e:
            print(f"[MIGRATE] Parse error: {e}")
            return
    count = 0
    for sid, s in sessions.items():
        t = s.get("translation", {})
        with _lock, _conn() as con:
            con.execute("""
                INSERT OR IGNORE INTO sessions
                    (session_id, phone, menu_choice, timestamp, status,
                     source_wav, recording_url, duration,
                     handled, note, translation)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sid,
                s.get("phone", ""),
                s.get("menu_choice", ""),
                s.get("timestamp", ""),
                s.get("status", "pending_call"),
                s.get("source_wav", ""),
                s.get("recording_url", ""),
                s.get("duration", ""),
                1 if s.get("handled") else 0,
                s.get("note", ""),
                json.dumps(t)
            ))
            con.commit()
        count += 1
    print(f"[MIGRATE] Imported {count} sessions from {json_path}")


# Legacy shims
def _load() -> dict:
    return {s["session_id"]: s for s in get_all_sessions()}


def _write(data: dict):
    for sid, s in data.items():
        t = s.get("translation", {})
        with _lock, _conn() as con:
            con.execute("""
                INSERT INTO sessions
                    (session_id, phone, timestamp, status,
                     handled, note, translation)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status      = excluded.status,
                    handled     = excluded.handled,
                    note        = excluded.note,
                    translation = excluded.translation
            """, (
                sid,
                s.get("phone", ""),
                s.get("timestamp", ""),
                s.get("status", "pending_call"),
                1 if s.get("handled") else 0,
                s.get("note", ""),
                json.dumps(t)
            ))
            con.commit()


# Initialise on import
init_db()
migrate_from_json()