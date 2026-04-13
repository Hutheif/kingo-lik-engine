# copilot.py — King'olik Co-Pilot using Groq Llama 3 70B
# Aviation-grade · State-aware · HITL learning · Graceful offline degradation
import os, json
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")
ELEVENLABS_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
VOICE_MODE     = os.environ.get("VOICE_MODE", "browser")  # browser | elevenlabs


def _get_snapshot() -> dict:
    """Reads kingolik.db and returns a live situational snapshot for the AI."""
    try:
        import sqlite3
        db = os.path.join(os.path.dirname(__file__), "kingolik.db")
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row

        total   = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM sessions WHERE status='pending_call'").fetchone()[0]
        handled = con.execute("SELECT COUNT(*) FROM sessions WHERE handled=1").fetchone()[0]
        gold    = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE correction!='' AND correction IS NOT NULL"
        ).fetchone()[0]

        cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
        recent = con.execute(
            "SELECT translation FROM sessions WHERE timestamp>? AND translation!=''", (cutoff,)
        ).fetchall()

        kw_counts = {}
        WATCH = ["water","maji","food","chakula","sick","mgonjwa","fire","moto",
                 "missing","violence","shelter","mtoto","child","akwap","njaa"]
        for row in recent:
            t = str(row[0]).lower()
            for kw in WATCH:
                if kw in t:
                    kw_counts[kw] = kw_counts.get(kw, 0) + 1

        top_kw = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        # Recent sessions for context
        last5 = con.execute("""
            SELECT phone, timestamp, translation, status FROM sessions
            ORDER BY timestamp DESC LIMIT 5
        """).fetchall()

        con.close()
        return {
            "total": total, "pending": pending, "handled": handled,
            "gold_pairs": gold, "top_keywords": top_kw,
            "last_sessions": [dict(r) for r in last5],
            "time": datetime.utcnow().strftime("%H:%M UTC")
        }
    except Exception as e:
        return {"error": str(e), "total": 0}


def _get_gold_corrections() -> str:
    """Returns recent caseworker corrections as ground truth for the AI."""
    try:
        import sqlite3
        db = os.path.join(os.path.dirname(__file__), "kingolik.db")
        con = sqlite3.connect(db)
        rows = con.execute("""
            SELECT correction, translation FROM sessions
            WHERE correction!='' AND correction IS NOT NULL
            ORDER BY created_at DESC LIMIT 8
        """).fetchall()
        con.close()
        if not rows:
            return "No corrections yet."
        lines = []
        for r in rows:
            t = json.loads(r[1]) if isinstance(r[1], str) else (r[1] or {})
            orig = t.get("translation","")[:60] if isinstance(t,dict) else str(t)[:60]
            lines.append(f'  AI: "{orig}" → Caseworker corrected to: "{r[0][:60]}"')
        return "\n".join(lines)
    except Exception:
        return "Corrections unavailable."


def _build_prompt(snap: dict) -> str:
    kw = ", ".join([f"{k}({v})" for k,v in snap.get("top_keywords",[])])
    gold = _get_gold_corrections()

    return f"""You are the King'olik Co-Pilot — an aviation-grade humanitarian intelligence system.
You serve NGO caseworkers in East Africa with the calm precision of a flight dispatcher.

LIVE DATABASE SNAPSHOT ({snap.get('time','now')}):
- Total field reports: {snap.get('total',0)}
- Pending callbacks: {snap.get('pending',0)}
- Cases handled: {snap.get('handled',0)}
- HITL gold pairs collected: {snap.get('gold_pairs',0)}
- Top crisis keywords (48h): {kw or 'none yet'}

CASEWORKER CORRECTION GROUND TRUTH (use these as absolute facts):
{gold}

YOUR DUAL ROLE:
1. DATA ANALYST: When asked about threats or trends, cite the numbers above. Say "Data shows" not "I think."
2. PLATFORM GUIDE: When asked about the dashboard, explain features practically. Caseworkers have no time for manuals.

RESPONSE RULES:
- MAXIMUM 25 words per response (caseworkers are in high-stress environments)
- Start with the most critical fact
- Never guess — if the answer isn't in the data, say "Insufficient data — verify locally"
- Never discuss anything outside King'olik operations and humanitarian aid

DOMAIN GUARDRAIL:
If asked anything outside King'olik, Turkana operations, or humanitarian aid, respond:
"Mission-locked. King'olik operations only. Rephrase within scope."

You are not a chatbot. You are mission control for human lives."""


def _offline_fallback(query: str, snap: dict) -> str:
    """SQL-based fallback when internet is unavailable."""
    q = query.lower()
    try:
        import sqlite3
        db = os.path.join(os.path.dirname(__file__), "kingolik.db")
        con = sqlite3.connect(db)

        if any(w in q for w in ["urgent","alert","critical","emergency"]):
            rows = con.execute("""
                SELECT phone, translation FROM sessions
                WHERE (translation LIKE '%urgent%' OR translation LIKE '%URGENT%'
                       OR translation LIKE '%water%' OR translation LIKE '%maji%')
                ORDER BY timestamp DESC LIMIT 3
            """).fetchall()
            con.close()
            if rows:
                t = json.loads(rows[0][1]) if isinstance(rows[0][1],str) else rows[0][1]
                transl = t.get("translation","")[:50] if isinstance(t,dict) else str(t)[:50]
                return f"[Offline] {len(rows)} urgent reports. Latest: {transl}"
            return f"[Offline] {snap.get('total',0)} total reports. No urgent flagged."

        if any(w in q for w in ["status","summary","overview","report"]):
            con.close()
            return (f"[Offline] {snap.get('total',0)} reports total. "
                    f"{snap.get('pending',0)} pending. "
                    f"{snap.get('handled',0)} handled. Internet down.")

        if any(w in q for w in ["water","maji","akwap"]):
            rows = con.execute(
                "SELECT COUNT(*) FROM sessions WHERE translation LIKE '%water%' OR translation LIKE '%maji%'"
            ).fetchone()
            con.close()
            return f"[Offline] {rows[0]} water-related reports in database."

        con.close()
        return f"[Offline] Database active. {snap.get('total',0)} reports. Cloud unavailable."
    except Exception as e:
        return f"[Offline] Database query failed: {e}"


def get_copilot_response(query: str) -> dict:
    """Main entry point. Returns text + optional audio."""
    snap = _get_snapshot()

    # ── Try Groq (fastest) ──────────────────────────────────
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)

        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": _build_prompt(snap)},
                {"role": "user", "content": query}
            ],
            temperature=0.3,
            max_tokens=80  # ~25 words — conserves ElevenLabs credits
        )
        text = completion.choices[0].message.content.strip()
        audio = _synthesize(text) if VOICE_MODE == "elevenlabs" else None

        return {"text": text, "audio": audio, "mode": "cloud", "snapshot": snap}

    except Exception as e:
        print(f"[COPILOT] Groq failed: {e} — offline fallback")
        text = _offline_fallback(query, snap)
        return {"text": text, "audio": None, "mode": "offline", "snapshot": snap}


def _synthesize(text: str) -> str | None:
    """ElevenLabs voice synthesis — only called when VOICE_MODE=elevenlabs."""
    if not ELEVENLABS_KEY:
        return None
    try:
        import requests
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VID}"
        headers = {"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2_5",  # lowest latency
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
        }
        r = requests.post(url, headers=headers, json=payload, timeout=8)
        if r.status_code == 200:
            fname = f"copilot_{datetime.utcnow().strftime('%H%M%S')}.mp3"
            path  = f"/tmp/{fname}"
            with open(path, "wb") as f:
                f.write(r.content)
            return f"/api/copilot/audio/{fname}"
    except Exception as e:
        print(f"[ELEVENLABS] {e}")
    return None


# ══ Flask routes — paste these into app.py ══
"""
from copilot import get_copilot_response

@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    data  = request.get_json()
    query = data.get("query", "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    result = get_copilot_response(query)
    return jsonify(result)

@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    import os
    path = f"/tmp/{filename}"
    if os.path.exists(path):
        from flask import send_file
        return send_file(path, mimetype="audio/mpeg")
    return jsonify({"error": "not found"}), 404
"""