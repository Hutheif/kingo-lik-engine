# copilot.py — King'olik Co-Pilot
# Provider: Groq (free default) or Anthropic (GITEX demo, set COPILOT_PROVIDER=anthropic)
# Scope-locked to King'olik humanitarian operations only.
# Offline SQL fallback when internet unavailable.

import os, json
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

PROVIDER       = os.environ.get("COPILOT_PROVIDER", "groq").lower()
GROQ_KEY       = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ELEVENLABS_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
VOICE_MODE     = os.environ.get("VOICE_MODE", "browser")
MAX_TOKENS     = 120


# ══════════════════════════════════════════════════════════════
#  Live snapshot — uses database.py correctly
#  Reads urgent_keywords from JSON field, not raw text search
# ══════════════════════════════════════════════════════════════
def _get_snapshot() -> dict:
    try:
        from database import get_all_sessions, _count_corrections
        sessions = get_all_sessions()
        cutoff   = datetime.utcnow() - timedelta(hours=48)

        total   = len(sessions)
        pending = sum(1 for s in sessions if s.get("status") == "pending_call")
        handled = sum(1 for s in sessions if s.get("handled"))
        urgent  = sum(1 for s in sessions
                      if ((s.get("translation") or {}).get("urgent_keywords") or []))
        gold    = _count_corrections()

        # Keyword frequency — reads parsed JSON translation field correctly
        WATCH = ["water","maji","food","chakula","sick","mgonjwa","fire","moto",
                 "missing","violence","shelter","mtoto","child","njaa","ngakipi",
                 "damu","jeraha","biyo","mach","weerar","gargaar","hatari","msaada"]
        kw_counts = {}
        recent_sessions = []
        for s in sessions:
            try:
                if datetime.fromisoformat(s.get("timestamp","")) >= cutoff:
                    recent_sessions.append(s)
                    t    = s.get("translation") or {}
                    # Read the actual parsed fields — not raw JSON string
                    text = (
                        (t.get("translation") or "") + " " +
                        (t.get("transcript") or "") + " " +
                        " ".join(t.get("urgent_keywords") or [])
                    ).lower()
                    for kw in WATCH:
                        if kw in text:
                            kw_counts[kw] = kw_counts.get(kw, 0) + 1
            except Exception:
                pass

        top_kw = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:6]

        # Last 5 sessions — privacy safe (last 4 digits of phone only)
        last5 = []
        for s in sessions[:5]:
            t = s.get("translation") or {}
            kws = t.get("urgent_keywords") or []
            last5.append({
                "phone":   ("..." + s.get("phone","")[-4:]) if s.get("phone") else "?",
                "ts":      s.get("timestamp","")[:16].replace("T"," "),
                "lang":    t.get("detected_language","?"),
                "summary": (t.get("translation","") or "no translation")[:100],
                "urgent":  bool(kws),
                "keywords": ", ".join(kws[:3]),
                "status":  s.get("status",""),
                "note":    s.get("note","")[:60]
            })

        return {
            "total": total, "pending": pending, "handled": handled,
            "urgent": urgent, "gold_pairs": gold,
            "top_keywords": top_kw, "last5": last5,
            "recent_count": len(recent_sessions),
            "time": datetime.utcnow().strftime("%H:%M UTC")
        }
    except Exception as e:
        return {
            "error": str(e), "total": 0, "pending": 0,
            "handled": 0, "urgent": 0, "gold_pairs": 0,
            "top_keywords": [], "last5": [], "recent_count": 0,
            "time": datetime.utcnow().strftime("%H:%M UTC")
        }


def _get_gold_corrections() -> str:
    try:
        import sqlite3
        db   = os.path.join(os.path.dirname(__file__), "kingolik.db")
        con  = sqlite3.connect(db)
        rows = con.execute("""
            SELECT correction, translation FROM sessions
            WHERE correction != '' AND correction IS NOT NULL
            ORDER BY created_at DESC LIMIT 6
        """).fetchall()
        con.close()
        if not rows:
            return "No caseworker corrections yet — system is learning."
        lines = []
        for correction, translation_json in rows:
            try:
                t    = json.loads(translation_json) if translation_json else {}
                orig = (t.get("translation","") if isinstance(t,dict) else "")[:60]
            except Exception:
                orig = ""
            lines.append(f'  AI: "{orig}" → Corrected: "{correction[:60]}"')
        return "\n".join(lines)
    except Exception:
        return "Corrections unavailable."


def _build_system_prompt(snap: dict) -> str:
    kw_str   = ", ".join([f"{k}({v})" for k, v in snap.get("top_keywords",[])])
    gold_str = _get_gold_corrections()
    last5_str = "\n".join([
        f"  [{s['ts']}] {s['phone']} | {s['lang']} | "
        f"{'URGENT:'+s['keywords'] if s['urgent'] else s['status']} | {s['summary']}"
        for s in snap.get("last5",[])
    ]) or "  No recent sessions."

    return f"""You are the King'olik Co-Pilot — a humanitarian field intelligence system for NGO caseworkers in Turkana West, Kenya.
You have DIRECT ACCESS to live field data. Speak like a calm flight dispatcher — precise, brief, actionable.

LIVE FIELD INTELLIGENCE ({snap.get('time','now')}):
Total reports: {snap.get('total',0)}
Pending callbacks: {snap.get('pending',0)}
Cases handled: {snap.get('handled',0)}
URGENT alerts: {snap.get('urgent',0)}
HITL training pairs: {snap.get('gold_pairs',0)}
Top crisis keywords (48h): {kw_str or 'none yet'}
Reports in last 48h: {snap.get('recent_count',0)}

LAST 5 FIELD REPORTS:
{last5_str}

CASEWORKER CORRECTIONS (treat as ground truth):
{gold_str}

STRICT RESPONSE PROTOCOL:
1. Maximum 3 sentences. No exceptions.
2. When citing numbers, say "Data shows X" — never "I think" or "probably".
3. If data is missing: "Insufficient data — verify locally."
4. SCOPE LOCK — you ONLY discuss: King'olik operations, Turkana field reports, humanitarian aid, dashboard features, translation data.
5. If asked about ANYTHING else (sport, cooking, news, celebrities, coding, personal advice):
   Respond EXACTLY: "Mission-locked. I only serve King'olik humanitarian operations."
6. Do not explain why you are refusing. Just state the lock and stop.

You are mission control. Lives depend on accuracy."""


# ══════════════════════════════════════════════════════════════
#  Offline SQL fallback — reads database directly when cloud down
# ══════════════════════════════════════════════════════════════
def _offline_fallback(query: str, snap: dict) -> str:
    q = query.lower()
    t = snap.get("total", 0)
    p = snap.get("pending", 0)
    h = snap.get("handled", 0)
    u = snap.get("urgent", 0)
    kw = ", ".join([k for k, _ in snap.get("top_keywords", [])])

    # Scope check even offline
    scope_words = ["king","olik","report","urgent","alert","water","food","session",
                   "handled","pending","translation","dashboard","field","case",
                   "maji","chakula","hatari","msaada","status","summary","keyword",
                   "training","correction","turkana","kakuma","ngo","humanitarian"]
    if not any(w in q for w in scope_words):
        return "Mission-locked. I only serve King'olik humanitarian operations."

    if any(w in q for w in ["urgent","alert","critical","emergency"]):
        return f"[Offline] Data shows {u} urgent alerts active. Top keywords: {kw or 'none'}. {p} calls pending dispatch."

    if any(w in q for w in ["water","maji","ngakipi","drought"]):
        return f"[Offline] Water-related reports detected in dataset. {p} pending response. Verify locally."

    if any(w in q for w in ["summary","status","overview","brief","report"]):
        return f"[Offline] {t} total reports. {p} pending. {h} handled. {u} urgent. Cloud unavailable — local data only."

    if any(w in q for w in ["correction","training","hitl","gold","pair"]):
        return f"[Offline] {snap.get('gold_pairs',0)} gold standard training pairs collected."

    if any(w in q for w in ["dashboard","how","explain","feature","export","csv"]):
        return "[Offline] Dashboard at /dashboard. Analytics at /analytics. Export CSV at /api/export/csv. Cloud unavailable."

    return f"[Offline] Database active. {t} reports, {u} urgent, {p} pending. Cloud unavailable."


# ══════════════════════════════════════════════════════════════
#  Main entry point
# ══════════════════════════════════════════════════════════════
def get_copilot_response(query: str) -> dict:
    snap   = _get_snapshot()
    system = _build_system_prompt(snap)

    # Quick scope pre-check — reject obvious off-topic before hitting API
    q_lower = query.lower()
    off_topic_signals = [
        "ronaldo","messi","football","soccer","recipe","cook","weather",
        "bitcoin","crypto","stock","dating","movie","netflix","music","song",
        "game","playstation","xbox","joke","story","poem","essay","homework"
    ]
    if any(sig in q_lower for sig in off_topic_signals):
        return {
            "text": "Mission-locked. I only serve King'olik humanitarian operations.",
            "audio": None, "mode": "scoped", "snapshot": snap
        }

    # ── Anthropic (GITEX demo mode — set COPILOT_PROVIDER=anthropic) ─
    if PROVIDER == "anthropic" and ANTHROPIC_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            msg    = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": query}]
            )
            text  = msg.content[0].text.strip()
            audio = _synthesize(text) if VOICE_MODE == "elevenlabs" else None
            return {"text": text, "audio": audio, "mode": "anthropic", "snapshot": snap}
        except Exception as e:
            print(f"[COPILOT] Anthropic failed: {e} — trying Groq")

    # ── Groq (free, default) ──────────────────────────────────
    if GROQ_KEY:
        try:
            from groq import Groq
            client     = Groq(api_key=GROQ_KEY)
            completion = client.chat.completions.create(
                model="llama3-70b-8192",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": query}
                ],
                temperature=0.2,
                max_tokens=MAX_TOKENS
            )
            text  = completion.choices[0].message.content.strip()
            audio = _synthesize(text) if VOICE_MODE == "elevenlabs" else None
            return {"text": text, "audio": audio, "mode": "groq", "snapshot": snap}
        except Exception as e:
            print(f"[COPILOT] Groq failed: {e} — offline fallback")

    # ── Offline SQL fallback ──────────────────────────────────
    text = _offline_fallback(query, snap)
    return {"text": text, "audio": None, "mode": "offline", "snapshot": snap}


def _synthesize(text: str):
    """ElevenLabs TTS — only when VOICE_MODE=elevenlabs."""
    if not ELEVENLABS_KEY or not text:
        return None
    try:
        import requests
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VID}",
            headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
            },
            timeout=8
        )
        if r.status_code == 200:
            fname = f"copilot_{datetime.utcnow().strftime('%H%M%S')}.mp3"
            path  = f"/tmp/{fname}"
            with open(path, "wb") as f:
                f.write(r.content)
            return f"/api/copilot/audio/{fname}"
    except Exception as e:
        print(f"[ELEVENLABS] {e}")
    return None