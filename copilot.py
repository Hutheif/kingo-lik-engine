# copilot.py — King'olik Co-Pilot
# FIX: llama3-70b-8192 decommissioned → llama-3.3-70b-versatile
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
GROQ_MODEL     = "llama-3.3-70b-versatile"  # replaces decommissioned llama3-70b-8192


def _get_snapshot() -> dict:
    try:
        from database import get_all_sessions, _count_corrections
        sessions = get_all_sessions()
        cutoff   = datetime.utcnow() - timedelta(hours=48)
        total    = len(sessions)
        pending  = sum(1 for s in sessions if s.get("status") == "pending_call")
        handled  = sum(1 for s in sessions if s.get("handled"))
        urgent   = sum(1 for s in sessions
                       if ((s.get("translation") or {}).get("urgent_keywords") or []))
        gold     = _count_corrections()
        WATCH    = ["water","maji","food","chakula","sick","mgonjwa","fire","moto",
                    "missing","violence","shelter","mtoto","child","njaa","damu",
                    "jeraha","hatari","msaada","attack","wezi","thieves"]
        kw_counts = {}
        recent = []
        for s in sessions:
            try:
                if datetime.fromisoformat(s.get("timestamp","")) >= cutoff:
                    recent.append(s)
                    t    = s.get("translation") or {}
                    text = ((t.get("translation") or "") + " " +
                            (t.get("transcript") or "") + " " +
                            " ".join(t.get("urgent_keywords") or [])).lower()
                    for kw in WATCH:
                        if kw in text:
                            kw_counts[kw] = kw_counts.get(kw, 0) + 1
            except Exception:
                pass
        top_kw = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:6]
        last5  = []
        for s in sessions[:5]:
            t   = s.get("translation") or {}
            kws = t.get("urgent_keywords") or []
            last5.append({
                "phone":    ("..." + s.get("phone","")[-4:]) if s.get("phone") else "?",
                "ts":       s.get("timestamp","")[:16].replace("T"," "),
                "lang":     t.get("detected_language","?"),
                "summary":  (t.get("translation","") or "no translation")[:100],
                "urgent":   bool(kws),
                "keywords": ", ".join(kws[:3]),
                "status":   s.get("status",""),
            })
        return {
            "total": total, "pending": pending, "handled": handled,
            "urgent": urgent, "gold_pairs": gold, "top_keywords": top_kw,
            "last5": last5, "recent_count": len(recent),
            "time": datetime.utcnow().strftime("%H:%M UTC")
        }
    except Exception as e:
        return {"error": str(e), "total": 0, "pending": 0, "handled": 0,
                "urgent": 0, "gold_pairs": 0, "top_keywords": [], "last5": [],
                "recent_count": 0, "time": datetime.utcnow().strftime("%H:%M UTC")}


def _get_gold_corrections() -> str:
    try:
        import sqlite3
        db   = os.path.join(os.path.dirname(__file__), "kingolik.db")
        con  = sqlite3.connect(db)
        rows = con.execute("""SELECT correction, translation FROM sessions
            WHERE correction != '' AND correction IS NOT NULL
            ORDER BY created_at DESC LIMIT 6""").fetchall()
        con.close()
        if not rows: return "No corrections yet."
        lines = []
        for correction, tj in rows:
            try:
                t = json.loads(tj) if tj else {}
                orig = (t.get("translation","") if isinstance(t,dict) else "")[:60]
            except Exception:
                orig = ""
            lines.append(f'  AI: "{orig}" → Corrected: "{correction[:60]}"')
        return "\n".join(lines)
    except Exception:
        return "Corrections unavailable."


def _build_system_prompt(snap: dict) -> str:
    kw_str    = ", ".join([f"{k}({v})" for k, v in snap.get("top_keywords",[])])
    gold_str  = _get_gold_corrections()
    last5_str = "\n".join([
        f"  [{s['ts']}] {s['phone']} | {s['lang']} | "
        f"{'URGENT:'+s['keywords'] if s['urgent'] else s['status']} | {s['summary']}"
        for s in snap.get("last5",[])
    ]) or "  No recent sessions."
    return f"""You are the King'olik Co-Pilot — humanitarian field intelligence for NGO caseworkers in Turkana West, Kenya.
Speak like a calm flight dispatcher: precise, brief, actionable.

LIVE FIELD INTELLIGENCE ({snap.get('time','now')}):
Total reports: {snap.get('total',0)} | Pending: {snap.get('pending',0)} | Handled: {snap.get('handled',0)} | Urgent: {snap.get('urgent',0)}
HITL training pairs: {snap.get('gold_pairs',0)} | Reports 48h: {snap.get('recent_count',0)}
Top crisis keywords: {kw_str or 'none'}

LAST 5 REPORTS:
{last5_str}

CASEWORKER CORRECTIONS (ground truth):
{gold_str}

RULES: Max 3 sentences. Say "Data shows" not "I think". Off-topic → "Mission-locked. King'olik operations only." """


def _offline_fallback(query: str, snap: dict) -> str:
    q = query.lower()
    if not any(w in q for w in ["king","olik","report","urgent","water","food",
                                  "status","handled","maji","msaada","humanitarian"]):
        return "Mission-locked. King'olik operations only."
    t=snap.get("total",0); p=snap.get("pending",0)
    h=snap.get("handled",0); u=snap.get("urgent",0)
    kw=", ".join([k for k,_ in snap.get("top_keywords",[])])
    if any(w in q for w in ["urgent","alert","critical"]):
        return f"Data shows {u} urgent alerts. Top keywords: {kw or 'none'}. {p} pending dispatch."
    if any(w in q for w in ["status","summary","overview"]):
        return f"Data shows {t} total, {p} pending, {h} handled, {u} urgent. Cloud unavailable."
    return f"Data shows {t} reports, {u} urgent, {p} pending. Cloud unavailable."


def get_copilot_response(query: str) -> dict:
    snap   = _get_snapshot()
    system = _build_system_prompt(snap)
    q_lower = query.lower()
    off_topic = ["ronaldo","messi","football","recipe","cook","weather","bitcoin",
                 "crypto","movie","netflix","music","song","game","xbox","joke"]
    if any(sig in q_lower for sig in off_topic):
        return {"text":"Mission-locked. King'olik operations only.","audio":None,"mode":"scoped","snapshot":snap}

    if GROQ_KEY:
        try:
            from groq import Groq
            completion = Groq(api_key=GROQ_KEY).chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role":"system","content":system},{"role":"user","content":query}],
                temperature=0.2, max_tokens=MAX_TOKENS
            )
            text  = completion.choices[0].message.content.strip()
            audio = _synthesize(text) if VOICE_MODE=="elevenlabs" else None
            return {"text":text,"audio":audio,"mode":"groq","snapshot":snap}
        except Exception as e:
            print(f"[COPILOT] Groq failed: {e} — offline fallback")

    if PROVIDER=="anthropic" and ANTHROPIC_KEY:
        try:
            import anthropic
            msg  = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
                model="claude-haiku-4-5", max_tokens=MAX_TOKENS,
                system=system, messages=[{"role":"user","content":query}])
            text = msg.content[0].text.strip()
            return {"text":text,"audio":None,"mode":"anthropic","snapshot":snap}
        except Exception as e:
            print(f"[COPILOT] Anthropic failed: {e}")

    return {"text":_offline_fallback(query,snap),"audio":None,"mode":"offline","snapshot":snap}


def _synthesize(text:str):
    if not ELEVENLABS_KEY or not text: return None
    try:
        import requests
        r=requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VID}",
            headers={"xi-api-key":ELEVENLABS_KEY,"Content-Type":"application/json"},
            json={"text":text,"model_id":"eleven_turbo_v2_5",
                  "voice_settings":{"stability":0.5,"similarity_boost":0.75}},timeout=8)
        if r.status_code==200:
            fname=f"copilot_{datetime.utcnow().strftime('%H%M%S')}.mp3"
            path=f"/tmp/{fname}"
            with open(path,"wb") as f: f.write(r.content)
            return f"/api/copilot/audio/{fname}"
    except Exception as e:
        print(f"[ELEVENLABS] {e}")
    return None