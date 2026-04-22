from gevent import monkey
monkey.patch_all()

from flask_socketio import SocketIO
from flask import Flask, request, make_response, jsonify, send_file, send_from_directory
import africastalking
import os, re, json, datetime, threading, logging, time
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

@app.route('/recordings/<path:filename>')
def serve_recording(filename):
    return send_from_directory('recordings', filename, as_attachment=False)

from dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)

from sync_queue import start_sync_worker
start_sync_worker()

# ══════════════════════════════════════════════════════════════
#  Africa's Talking init
# ══════════════════════════════════════════════════════════════
AT_USERNAME        = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY         = os.getenv("AT_API_KEY", "")
YOUR_NUMBER        = os.getenv("AT_VIRTUAL_NUMBER", "+254711082547")
BASE_URL           = os.getenv("BASE_URL", "https://kingo-lik-engine.onrender.com").rstrip("/")
GREETING_AUDIO_URL = os.getenv("GREETING_AUDIO_URL", "")

_raw_alert  = os.getenv("ALERT_PHONE", "")
BLACKLISTED = {"+254706648650"}
ALERT_PHONE = "" if _raw_alert in BLACKLISTED else _raw_alert

voice_service = None
sms_service   = None

if AT_API_KEY:
    africastalking.initialize(username=AT_USERNAME, api_key=AT_API_KEY)
    voice_service = africastalking.Voice
    sms_service   = africastalking.SMS
    print(f"[AT] OK  username={AT_USERNAME}  number={YOUR_NUMBER}")
else:
    print("[AT] WARNING — no AT_API_KEY")

print(f"[OK] BASE_URL={BASE_URL}")
print(f"[ALERT] {ALERT_PHONE or 'SMS alerts disabled'}")


# ══════════════════════════════════════════════════════════════
#  Logger
# ══════════════════════════════════════════════════════════════
class Log:
    R="\033[0m"; B="\033[1m"; D="\033[2m"
    G="\033[32m"; Y="\033[33m"; Re="\033[31m"; C="\033[36m"; W="\033[37m"

    @staticmethod
    def _t(): return datetime.datetime.now().strftime("%H:%M:%S")

    @classmethod
    def info(cls,m): print(f"{cls.D}{cls._t()}{cls.R}  {cls.C}INFO{cls.R} {m}")
    @classmethod
    def ok(cls,m):   print(f"{cls.D}{cls._t()}{cls.R}  {cls.G}{cls.B}OK  {cls.R} {m}")
    @classmethod
    def warn(cls,m): print(f"{cls.D}{cls._t()}{cls.R}  {cls.Y}WARN{cls.R} {m}")
    @classmethod
    def error(cls,m):print(f"{cls.D}{cls._t()}{cls.R}  {cls.Re}ERR {cls.R} {m}")
    @classmethod
    def ussd(cls,sid,phone,text):
        s=(sid or"?")[-8:]
        print(f"{cls.D}{cls._t()}{cls.R}  {cls.B}USSD{cls.R}  {cls.W}{phone}{cls.R}  [...{s}]  '{text}'")
    @classmethod
    def divider(cls): print(f"{cls.D}{'─'*54}{cls.R}")
    @classmethod
    def section(cls,t): print(f"\n{cls.D}┌── {t}{cls.R}\n")


# ══════════════════════════════════════════════════════════════
#  Phone normalisation
# ══════════════════════════════════════════════════════════════
def _norm(phone: str) -> str:
    if not phone: return ""
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("+254"): return p
    if p.startswith("254") and len(p) >= 12: return "+" + p
    if p.startswith("0") and len(p) == 10: return "+254" + p[1:]
    return p


# ══════════════════════════════════════════════════════════════
#  Whitelist
#
#  ALLOWED_PHONES in .env controls who can:
#    - Use the USSD code (*789*1990#)
#    - Flash-call the number and get called back
#
#  SMS emergency beacon is OPEN TO EVERYONE — a refugee in
#  distress must never be blocked by a whitelist.
# ══════════════════════════════════════════════════════════════
_raw_wl = os.getenv("ALLOWED_PHONES", "")
ALLOWED: set = set(
    _norm(p.strip()) for p in _raw_wl.split(",") if p.strip()
) if _raw_wl else set()

if ALLOWED:
    print(f"[SEC] Whitelist active: {sorted(ALLOWED)}")
else:
    print("[SEC] No ALLOWED_PHONES set — all numbers permitted (open mode)")


def _allowed(phone: str) -> bool:
    """
    Returns True if this phone number is allowed to use USSD and flash calls.
    If no whitelist is configured, everyone is allowed (open mode).
    """
    if not ALLOWED:
        return True  # open mode — no whitelist configured
    return _norm(phone) in ALLOWED


# ══════════════════════════════════════════════════════════════
#  Rate limiter — prevents spam, 10 calls per hour per number
#  Whitelisted numbers are exempt from rate limiting.
# ══════════════════════════════════════════════════════════════
_rs = defaultdict(list)
_rl = threading.Lock()
RLIMIT = 10
RWIN   = 3600  # 1 hour window


def _limited(phone: str) -> bool:
    """
    Returns True if this number has exceeded the rate limit.
    Whitelisted numbers are never rate-limited.
    """
    phone = _norm(phone)
    if phone in ALLOWED:
        return False  # whitelist is always exempt
    now = time.time()
    with _rl:
        _rs[phone] = [t for t in _rs[phone] if now - t < RWIN]
        if len(_rs[phone]) >= RLIMIT:
            return True
        _rs[phone].append(now)
        return False


# ══════════════════════════════════════════════════════════════
#  Urgent keywords (USSD + SMS detection)
# ══════════════════════════════════════════════════════════════
URGENT_KW = [
    "help","msaada","haraka","emergency","hatari","moto","fire",
    "damu","blood","jeraha","injury","attack","shambulio","wezi",
    "thieves","robbery","vita","violence","mgonjwa","sick","hospital",
    "njaa","hunger","maji","water","missing","kupotea","police","polisi",
    "ninavamiwa","navamiwa","attacked","danger","sos","mjamzito","pregnant",
    "chakula","food","gargaar","degdeg","weerar","dab","dhiig",
    "apese","ngosi","ekisil","tukoi","abakare",
]


def _kws(text: str) -> list:
    t = text.lower()
    return list(set(k for k in URGENT_KW if k in t))


# ══════════════════════════════════════════════════════════════
#  SMS emergency keyword set — broader than USSD keywords
#  Open to ALL callers regardless of whitelist
# ══════════════════════════════════════════════════════════════
SMS_BEACON_KEYWORDS = {
    "help","sos","emergency","fire","attack","blood","injured","sick",
    "danger","missing","flood","thief","thieves","robbery","violence",
    "hurt","dying","dead","stuck","trapped","rape","abuse","mayday",
    "urgent","critical",
    "msaada","haraka","hatari","moto","damu","wezi","vita","jeraha",
    "mgonjwa","kupotea","njaa","maji","saidia","dharura","navamiwa",
    "ninavamiwa","shambulio","bunduki","kisu","kuumia","tafadhali",
    "gargaar","degdeg","khatar","weerar","dab","dhiig","baahi",
    "apese","ngosi","ekisil","tukoi","abakare",
}


def _is_emergency_sms(text: str) -> bool:
    t = text.lower().strip()
    words = re.findall(r'\w+', t)
    for word in words:
        if word in SMS_BEACON_KEYWORDS:
            return True
    return t in SMS_BEACON_KEYWORDS


# ══════════════════════════════════════════════════════════════
#  Hallucination detector
#  Catches Whisper garbage output on silent/IVR/noisy audio
# ══════════════════════════════════════════════════════════════
def _is_hallucination(text: str) -> bool:
    if not text or len(text.strip()) < 3:
        return True
    t = text.strip()
    words = re.findall(r'\b\w+\b', t.lower())
    if not words:
        return True
    unique = set(words)
    # More than 60% repeated words = hallucination
    if len(words) > 4 and len(unique) / len(words) < 0.4:
        return True
    # Mostly non-Latin characters (Chinese dots, Arabic repeated) = hallucination
    non_latin = re.findall(r'[^\x00-\x7F]', t)
    if len(non_latin) > len(t) * 0.5:
        return True
    # Single word repeated excessively
    if len(words) > 6:
        from collections import Counter
        most_common_count = Counter(words).most_common(1)[0][1]
        if most_common_count / len(words) > 0.6:
            return True
    return False


# ══════════════════════════════════════════════════════════════
#  Telecom IVR noise filter
# ══════════════════════════════════════════════════════════════
TELECOM_NOISE_PHRASES = [
    "please try again", "nambari uliupiga", "nambari uliupida",
    "ina tumika kwa sasa", "tafadali jaribu tena", "ethio telecom",
    "all lines are currently", "mteja wa laini", "the number you have dialed",
    "quickly busy", "hakuna mteja", "imezimwa", "come on come on",
]


def _is_telecom_noise(text: str) -> bool:
    if not text:
        return True
    t = text.lower().strip()
    return any(phrase in t for phrase in TELECOM_NOISE_PHRASES)


# ══════════════════════════════════════════════════════════════
#  Session state — defined ONCE
# ══════════════════════════════════════════════════════════════
_answered:  set  = set()   # AT session IDs already served a greeting
_pending:   dict = {}      # phone → our session_id (waiting for outbound answer)
_at_to_our: dict = {}      # AT sessionId → our session_id (for recording lookup)


# ══════════════════════════════════════════════════════════════
#  XML helpers
# ══════════════════════════════════════════════════════════════
def _xml(body: str):
    r = make_response(body, 200)
    r.headers["Content-Type"] = "application/xml"
    return r


def _hangup():
    return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>')


def _greeting(session_id: str):
    cb = f"{BASE_URL}/voice/save?session_id={session_id}"
    if GREETING_AUDIO_URL:
        v = f'<Play>{GREETING_AUDIO_URL}</Play>'
    else:
        v = (
            '<Say voice="woman" playBeep="false">'
            'Asante. This is King-olik. '
            'We are listening, and we are here to help you. '
            'Your voice is confidential and will only be used to rescue you. '
            'Please take a breath, report the issue, then hang up.'
            '</Say>'
        )
    return _xml(f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {v}
  <Record finishOnKey="#*" maxLength="120" trimSilence="true"
          playBeep="true" callbackUrl="{cb}"/>
</Response>""")


def _empty():
    return make_response("", 200)


def _tr(b: str):
    r = make_response(b, 200)
    r.headers["Content-Type"] = "text/plain"
    return r


# ══════════════════════════════════════════════════════════════
#  Werkzeug log filter — suppress noisy polling routes
# ══════════════════════════════════════════════════════════════
class _PF(logging.Filter):
    def filter(self, r):
        m = r.getMessage()
        return not any(x in m for x in [
            '"/api/sessions', '"/api/audio/', '"/favicon', '"/api/analytics'
        ])


logging.getLogger("werkzeug").addFilter(_PF())


@app.before_request
def _lr():
    skip = {'/api/sessions', '/dashboard', '/analytics', '/favicon.ico'}
    if request.path in skip or request.path.startswith('/api/audio/'):
        return
    a = {k: v for k, v in request.args.items() if k != 't'}
    Log.info(f"{request.method} {request.path}" + (f" {a}" if a else ""))


# ══════════════════════════════════════════════════════════════
#  USSD handler
#  Only whitelisted numbers can access the USSD menu.
# ══════════════════════════════════════════════════════════════
@app.route("/ussd", methods=["POST"])
def ussd():
    from database import save_session
    sid   = request.form.get("sessionId", "")
    phone = _norm(request.form.get("phoneNumber", ""))
    text  = request.form.get("text", "").strip()
    Log.ussd(sid, phone, text)

    if not _allowed(phone):
        Log.warn(f"USSD blocked — not whitelisted: {phone}")
        return _tr("END Huduma hii inahitaji idhini.\nThis service requires authorisation.")

    parts = text.split("*") if text else []

    if text == "":
        return _tr("CON Karibu King'olik.\n1. Afya/Health\n2. Chakula/Food\n3. Usalama/Security\n4. Ingine/Other")

    if text in ("1", "2", "3"):
        lbl = {"1": "Afya/Health", "2": "Chakula/Food", "3": "Usalama/Security"}
        return _tr(f"CON {lbl[text]}\nTutakupigia ndani ya sekunde 10.\n1. Thibitisha/Confirm\n0. Ghairi/Cancel")

    if text == "4":
        return _tr("CON Andika shida kwa ufupi:\nType your issue briefly:")

    if text.startswith("4*") and len(parts) >= 2:
        raw = "*".join(parts[1:])
        save_session({
            "session_id": sid, "phone": phone, "menu_choice": "4",
            "timestamp": datetime.datetime.utcnow().isoformat(), "status": "text_report"
        })
        Log.ok(f"Text report [{phone}] '{raw[:40]}' — translating...")
        threading.Thread(target=_translate_text, args=(sid, raw, phone), daemon=True).start()
        return _tr("END Ahsante. Tumepokea ripoti yako.\nThank you. Report received.")

    if text.endswith("*1"):
        mc = parts[0] if parts else "1"
        save_session({
            "session_id": sid, "phone": phone, "menu_choice": mc,
            "timestamp": datetime.datetime.utcnow().isoformat(), "status": "pending_call"
        })
        Log.ok(f"USSD confirm [{phone}] menu={mc}")
        socketio.start_background_task(_call_back, phone, sid)
        return _tr("END Asante! Tutakupigia sekunde 10.\nThank you! Calling you back in 10 seconds. FREE.")

    if text.endswith("*0"):
        return _tr("END Umeghairi.\nCancelled. Dial *789*1990# anytime.")

    return _tr("END Chaguo batili.\nInvalid choice. Dial *789*1990#")


# ══════════════════════════════════════════════════════════════
#  Text translation (USSD option 4 + SMS beacon)
#  Uses Gemini 2.0 Flash with fast-fail on quota errors.
# ══════════════════════════════════════════════════════════════
def _translate_text(session_id: str, raw: str, phone: str):
    kws        = _kws(raw)
    translation = raw
    lang        = "sw"
    conf        = "medium"
    engine      = "ussd_text"

    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        for attempt in range(2):  # max 2 attempts
            try:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=key)
                prompt = (
                    "Translate this humanitarian field report to English. "
                    "It may be Swahili, Turkana, Somali, Arabic, or mixed. "
                    'Return ONLY JSON with no extra text: '
                    '{"detected_language":"sw","translation":"...","urgent_keywords":[],"confidence":"high"}\n\n'
                    f"Text: {raw}"
                )
                resp = client.models.generate_content(
                    model="models/gemini-2.0-flash",
                    contents=[types.Content(parts=[types.Part(text=prompt)])]
                )
                r = re.sub(r"```json|```", "", resp.text.strip()).strip()
                m = re.search(r'\{.*\}', r, re.DOTALL)
                if m:
                    p           = json.loads(m.group())
                    translation = p.get("translation", raw)
                    lang        = p.get("detected_language", "sw")
                    kws         = list(set(kws + (p.get("urgent_keywords") or [])))
                    conf        = p.get("confidence", "medium")
                    engine      = "gemini_text"
                    Log.ok(f"Gemini translated [{session_id[-8:]}]: '{translation[:60]}'")
                    break

            except Exception as e:
                err = str(e)
                if "429" in err or "503" in err or "RESOURCE_EXHAUSTED" in err:
                    Log.warn(f"Gemini quota/overload — using raw text [{session_id[-8:]}]")
                    break  # don't retry, save raw text immediately
                Log.warn(f"Gemini attempt {attempt+1} failed: {e}")
                if attempt == 1:
                    break

    result = {
        "transcript":        raw,
        "detected_language": lang,
        "translation":       translation,
        "urgent_keywords":   kws,
        "confidence":        conf,
        "engine":            engine,
        "requires_review":   conf == "low",
        "is_text_report":    True,
    }
    from database import save_translation
    save_translation(session_id, result)
    Log.ok(f"Text saved [{session_id[-8:]}]  lang={lang}  translation='{translation[:50]}'")
    if kws and ALERT_PHONE:
        threading.Thread(target=_alert_sms, args=(session_id, phone, result), daemon=True).start()


# ══════════════════════════════════════════════════════════════
#  Alert SMS to coordinator
# ══════════════════════════════════════════════════════════════
def _alert_sms(session_id: str, caller: str, result: dict):
    if not sms_service or not ALERT_PHONE:
        return
    try:
        kws  = result.get("urgent_keywords", [])
        t    = result.get("translation", "")[:100]
        msg  = (
            f"KINGOLIK URGENT\n"
            f"From:{caller}\n"
            f"Alert:{','.join(kws[:5])}\n"
            f"Said:{t}\n"
            f"Ref:{session_id[-8:]}"
        )
        resp    = sms_service.send(message=msg, recipients=[ALERT_PHONE])
        recips  = resp.get("SMSMessageData", {}).get("Recipients", [])
        status  = recips[0].get("status", "?") if recips else "no_recipients"
        if status == "Success":
            Log.ok(f"Alert SMS → {ALERT_PHONE}")
        else:
            Log.warn(f"Alert SMS status={status}")
    except Exception as e:
        Log.error(f"Alert SMS: {e}")


# ══════════════════════════════════════════════════════════════
#  Outbound callback — calls the user back after USSD or flash
# ══════════════════════════════════════════════════════════════
def _call_back(phone: str, session_id: str, max_attempts: int = 3):
    phone = _norm(phone)
    for attempt in range(1, max_attempts + 1):
        wait = 3 if attempt == 1 else 30
        Log.info(f"Callback wait={wait}s [{session_id[-8:]}] → {phone}  attempt={attempt}/{max_attempts}")
        time.sleep(wait)

        if not voice_service:
            Log.warn("No voice_service — audio fallback")
            _fallback(phone, session_id)
            return

        _pending[phone] = session_id
        Log.info(f"Stored _pending {phone} → {session_id[-8:]}")

        try:
            resp    = voice_service.call(callFrom=YOUR_NUMBER, callTo=[phone])
            entries = resp.get("entries", [])
            status  = entries[0].get("status", "?") if entries else "no_entries"
            Log.ok(f"Call placed status={status} phone={phone} attempt={attempt}")
            if status in ("Queued", "Ringing", "Success"):
                return
            Log.warn(f"Bad call status '{status}' attempt {attempt}")
            if attempt == max_attempts:
                _fallback(phone, session_id)
        except Exception as e:
            Log.error(f"voice.call attempt {attempt}: {e}")
            if attempt == max_attempts:
                _fallback(phone, session_id)


def _fallback(phone: str, session_id: str):
    wav = _wav()
    if not wav:
        Log.warn(f"No WAV for fallback [{session_id[-8:]}]")
        return
    Log.info(f"Audio fallback [{session_id[-8:]}] {os.path.basename(wav)}")
    def run():
        time.sleep(1)
        from translator import process_recording
        process_recording(session_id, wav, phone)
    threading.Thread(target=run, daemon=True).start()


# ══════════════════════════════════════════════════════════════
#  /voice/answer — handles all inbound and outbound call events
#
#  WHITELIST LOGIC:
#  - Inbound (flash) calls from WHITELISTED numbers → hang up + call back
#  - Inbound calls from NON-WHITELISTED numbers → hang up (no callback)
#  - Outbound answered → serve greeting + record
# ══════════════════════════════════════════════════════════════
@app.route("/voice/answer", methods=["POST", "GET"])
def voice_answer():
    caller    = _norm(request.values.get("callerNumber", "") or "")
    dest      = _norm(request.values.get("destinationNumber", "") or "")
    direction = (request.values.get("direction", "") or "").lower()
    state     = (request.values.get("callSessionState", "") or "")
    is_active = (request.values.get("isActive", "0") or "0")
    at_sid    = (request.values.get("sessionId", "") or "")

    rec_url = (
        request.form.get("recordingUrl") or request.form.get("RecordingUrl") or
        request.values.get("recordingUrl") or request.values.get("RecordingUrl") or ""
    )
    dur = (
        request.form.get("durationInSeconds") or
        request.values.get("durationInSeconds") or "0"
    )

    Log.info(
        f"VOICE caller={caller} dest={dest} dir={direction} "
        f"state={state} isActive={is_active} rec={'YES' if rec_url else 'no'} "
        f"atSid=[...{at_sid[-8:] if at_sid else '?'}]"
    )

    # ── Recording arrived ─────────────────────────────────────
    if rec_url:
        Log.ok(f"Recording received dur={dur}s")
        user_phone = dest if dest != YOUR_NUMBER else caller
        sid = (
            request.args.get("session_id") or
            _at_to_our.pop(at_sid, None) or
            _pending.pop(user_phone, None) or
            _pending.pop(caller, None) or
            at_sid or
            f"rec_{datetime.datetime.utcnow().strftime('%H%M%S')}"
        )
        _handle_recording(sid, rec_url, dur)
        return _hangup()

    # ── Inactive call ─────────────────────────────────────────
    if is_active != "1":
        Log.info(f"isActive=0 state={state} — ignored")
        return _hangup()

    # ── Inbound (flash) call ──────────────────────────────────
    #    Whitelisted: hang up + call back in 3s (free for them)
    #    Not whitelisted: just hang up
    if direction == "inbound":
        if not caller:
            return _hangup()

        if not _allowed(caller):
            Log.warn(f"Inbound from non-whitelisted {caller} — hanging up (no callback)")
            return _hangup()

        if _limited(caller):
            Log.warn(f"Rate limit hit for {caller}")
            return _hangup()

        new_sid = f"flash_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
        from database import save_session
        save_session({
            "session_id": new_sid,
            "phone":      caller,
            "menu_choice": "flash",
            "timestamp":  datetime.datetime.utcnow().isoformat(),
            "status":     "pending_call",
        })
        Log.ok(f"Flash call from whitelisted {caller} — callback in 3s")
        socketio.start_background_task(_call_back, caller, new_sid)
        return _hangup()  # hang up their call; we call them back

    # ── Outbound answered → serve greeting ───────────────────
    if at_sid in _answered:
        Log.info(f"Duplicate answer [{at_sid[-8:]}] — ignored")
        return _hangup()

    _answered.add(at_sid)

    user_phone = dest if dest != YOUR_NUMBER else caller
    our_sid = (
        _pending.pop(user_phone, None) or
        _pending.pop(caller, None) or
        _pending.pop(dest, None) or
        f"ans_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    )
    _at_to_our[at_sid] = our_sid

    Log.ok(f"Outbound answered user={user_phone} session={our_sid[-8:]}")
    return _greeting(our_sid)


# ══════════════════════════════════════════════════════════════
#  /voice/save — explicit recording callback from AT
# ══════════════════════════════════════════════════════════════
@app.route("/voice/save", methods=["POST", "GET"])
def voice_save():
    Log.ok("VOICE/SAVE HIT ← recording from AT")
    at_sid = (request.values.get("sessionId") or "")
    sid = (
        request.args.get("session_id") or
        _at_to_our.pop(at_sid, None) or
        request.values.get("sessionId") or ""
    )
    url = (
        request.form.get("recordingUrl") or request.form.get("RecordingUrl") or
        request.values.get("recordingUrl") or request.values.get("RecordingUrl") or
        request.args.get("recordingUrl") or ""
    )
    dur = (request.form.get("durationInSeconds") or request.values.get("durationInSeconds") or "0")
    Log.info(f"  our_sid={sid or 'NONE'}  url={'YES '+dur+'s' if url else 'NONE'}")

    if url:
        _handle_recording(sid, url, dur)
    else:
        Log.warn("No recordingUrl in /voice/save — user may not have pressed #")

    return _empty()


def _handle_recording(session_id: str, recording_url: str, duration: str = "0"):
    from database import update_call_record, save_audio_url
    if session_id:
        update_call_record(session_id, recording_url, duration)

    sid = session_id or f"rec_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    Log.ok(f"Processing recording [{sid[-8:]}]  dur={duration}s")

    def run():
        recordings_dir = os.path.join(os.getcwd(), "recordings")
        os.makedirs(recordings_dir, exist_ok=True)
        local_path = os.path.join(recordings_dir, f"{sid}_raw.wav")
        downloaded = False

        if recording_url.startswith("http"):
            try:
                import requests as req
                Log.info(f"Downloading [{sid[-8:]}] ...")
                headers = {}
                if AT_API_KEY:
                    import base64
                    creds = base64.b64encode(f"{AT_USERNAME}:{AT_API_KEY}".encode()).decode()
                    headers["Authorization"] = f"Basic {creds}"
                r = req.get(recording_url, headers=headers, timeout=30)
                if r.status_code == 200:
                    with open(local_path, "wb") as f:
                        f.write(r.content)
                    downloaded = True
                    Log.ok(f"Saved [{sid[-8:]}] → {os.path.basename(local_path)}")
                else:
                    Log.warn(f"Download HTTP {r.status_code}")
            except Exception as e:
                Log.error(f"Download failed: {e}")
        elif os.path.exists(recording_url):
            import shutil
            try:
                if os.path.abspath(recording_url) != os.path.abspath(local_path):
                    shutil.copy2(recording_url, local_path)
                else:
                    local_path = recording_url
                downloaded = True
                Log.ok(f"Copied local → {os.path.basename(local_path)}")
            except Exception as e:
                Log.error(f"File copy failed: {e}")
                local_path = recording_url
                downloaded = True

        if not downloaded:
            Log.error(f"Could not get audio [{sid[-8:]}]")
            return

        try:
            save_audio_url(sid, f"/api/audio/{sid}")
        except Exception:
            pass

        try:
            from translator import process_recording
            process_recording(sid, local_path)
            Log.ok(f"Translation pipeline started [{sid[-8:]}]")
        except Exception as e:
            Log.error(f"Translation failed [{sid[-8:]}]: {e}")

    threading.Thread(target=run, daemon=True).start()


# ══════════════════════════════════════════════════════════════
#  /sms — SMS Beacon
#
#  OPEN TO EVERYONE — no whitelist check.
#  A refugee sending "HELP" must never be blocked.
#
#  If message contains emergency keywords:
#    1. Save as immediate dashboard report
#    2. Send confirmation SMS back
#    3. Call them back in 10 seconds
#
#  If no keywords (plain missed call signal):
#    1. Send confirmation SMS
#    2. Call them back in 10 seconds
# ══════════════════════════════════════════════════════════════
@app.route("/sms", methods=["POST", "GET"])
def sms():
    sender = _norm(request.values.get("from") or request.values.get("fromNumber") or "")
    text   = (request.values.get("text") or "").strip()
    Log.info(f"SMS from={sender}  text='{text[:80]}'")

    if not sender:
        return _empty()

    # Rate limit non-whitelisted senders to prevent SMS spam abuse
    if _limited(sender):
        Log.warn(f"SMS rate limit: {sender}")
        return _empty()

    is_emergency = _is_emergency_sms(text)
    kws = _kws(text)
    sid = f"sms_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"

    from database import save_session
    save_session({
        "session_id": sid,
        "phone":      sender,
        "menu_choice": "sms_beacon",
        "timestamp":  datetime.datetime.utcnow().isoformat(),
        "status":     "pending_call",
    })

    if is_emergency and text:
        Log.ok(f"SMS EMERGENCY [{kws}] from {sender} — saving + callback")
        threading.Thread(target=_translate_text, args=(sid, text, sender), daemon=True).start()
    else:
        Log.ok(f"SMS beacon from {sender} — callback triggered")

    threading.Thread(
        target=_send_confirmation_sms, args=(sender, is_emergency, sid), daemon=True
    ).start()

    socketio.start_background_task(_call_back, sender, sid)
    return _empty()


def _send_confirmation_sms(sender: str, is_emergency: bool, session_id: str):
    if not sms_service:
        return
    try:
        if is_emergency:
            msg = (
                f"King'olik: Tumepokea ujumbe wako wa dharura.\n"
                f"We received your emergency message.\n"
                f"Tutakupigia simu ndani ya sekunde 10. FREE.\n"
                f"Ref: {session_id[-6:]}"
            )
        else:
            msg = (
                f"King'olik: Ujumbe wako umepokelewa.\n"
                f"Your message received.\n"
                f"Tutakupigia simu bure hivi karibuni.\n"
                f"We will call you back shortly. FREE."
            )
        resp   = sms_service.send(message=msg, recipients=[sender])
        recips = resp.get("SMSMessageData", {}).get("Recipients", [])
        status = recips[0].get("status", "?") if recips else "no_recipients"
        Log.ok(f"Confirmation SMS → {sender}  status={status}")
    except Exception as e:
        Log.error(f"Confirmation SMS failed: {e}")


# ══════════════════════════════════════════════════════════════
#  WAV picker — finds most recent recording for fallback
# ══════════════════════════════════════════════════════════════
def _wav():
    d = os.path.join(os.getcwd(), "recordings")
    try:
        files = [
            os.path.join(d, f) for f in os.listdir(d)
            if f.endswith(".wav")
            and "_clean" not in f
            and "_raw_clean" not in f
            and not any(f.startswith(p) for p in ("ATUid_", "local_test_", "url_test_"))
        ]
        return max(files, key=os.path.getmtime) if files else None
    except Exception as e:
        Log.error(f"WAV scan: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  Test routes
# ══════════════════════════════════════════════════════════════
@app.route("/test/local")
def test_local():
    from translator import process_recording
    sid = request.args.get("session_id", f"local_{datetime.datetime.utcnow().strftime('%H%M%S')}")
    p   = (os.path.join(os.getcwd(), request.args.get("file")) if request.args.get("file") else _wav())
    if not p or not os.path.exists(p):
        return jsonify({"error": "No WAV"}), 404
    threading.Thread(target=process_recording, args=(sid, p), daemon=True).start()
    return jsonify({"status": "processing", "session_id": sid, "file": os.path.basename(p)}), 200


@app.route("/test/file/<filename>")
def test_file(filename):
    from translator import process_recording
    sid = f"local_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    p   = os.path.join(os.getcwd(), "recordings", filename)
    if not os.path.exists(p):
        available = [f for f in os.listdir("recordings") if f.endswith(".wav") and "_clean" not in f]
        return jsonify({"error": f"{filename} not found", "available": available}), 404
    threading.Thread(target=process_recording, args=(sid, p), daemon=True).start()
    return jsonify({"status": "processing", "file": filename, "session_id": sid}), 200


@app.route("/test/call/<path:phone>")
def test_call(phone):
    phone = _norm(phone)
    if not voice_service:
        return jsonify({"error": "AT_API_KEY not set"}), 500
    sid = f"test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({
        "session_id": sid, "phone": phone, "menu_choice": "test",
        "timestamp": datetime.datetime.utcnow().isoformat(), "status": "pending_call"
    })
    threading.Thread(target=_call_back, args=(phone, sid), daemon=True).start()
    return jsonify({
        "status": "calling", "phone": phone, "session": sid,
        "step1": "Answer in ~3 seconds",
        "step2": "Hear greeting + beep",
        "step3": "Speak your message in Swahili/English/Arabic",
        "step4": "Press # to finish recording",
        "step5": "Watch terminal: VOICE/SAVE HIT",
        "step6": "Translation appears on /dashboard",
    }), 200


@app.route("/test/sim-save")
def test_sim_save():
    sid = request.args.get("session_id", "")
    if not sid:
        from database import get_all_sessions
        sessions = get_all_sessions()
        recent   = [s for s in sessions if s.get("status") in ("pending_call", "recorded")]
        sid      = recent[0]["session_id"] if recent else f"simtest_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    wav = _wav()
    if not wav:
        return jsonify({"error": "No WAV in recordings/"}), 404
    Log.ok(f"Sim-save [{sid[-8:]}] {os.path.basename(wav)}")
    _handle_recording(sid, wav, "15")
    return jsonify({"status": "Pipeline test started", "session_id": sid,
                    "source": os.path.basename(wav), "watch": "/dashboard"}), 200


@app.route("/test/sms/<phone>")
def test_sms(phone):
    phone = _norm(phone)
    sid   = f"sms_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({
        "session_id": sid, "phone": phone, "menu_choice": "sms_test",
        "timestamp": datetime.datetime.utcnow().isoformat(), "status": "pending_call"
    })
    socketio.start_background_task(_call_back, phone, sid)
    return jsonify({"status": "callback triggered", "phone": phone, "session": sid}), 200


@app.route("/test/at-config")
def at_config():
    return jsonify({
        "AT_voice_callback_url": f"{BASE_URL}/voice/answer",
        "AT_ussd_callback_url":  f"{BASE_URL}/ussd",
        "AT_sms_callback_url":   f"{BASE_URL}/sms",
        "recording_callback":    f"{BASE_URL}/voice/save",
        "test_call":             f"{BASE_URL}/test/call/+YOUR_PHONE",
        "status": {
            "ALERT_PHONE": ALERT_PHONE or "NOT SET",
            "BASE_URL":    BASE_URL,
            "whitelist":   sorted(ALLOWED) if ALLOWED else "open (all phones allowed)",
        }
    })


@app.route("/admin/fix-hallucinations")
def fix_hallucinations():
    import sqlite3
    con = sqlite3.connect("kingolik.db")
    con.execute("UPDATE sessions SET translation='', status='recorded' WHERE translation LIKE '%י%'")
    con.commit()
    con.close()
    return "Cleared hallucinated translations. Refresh dashboard."


# ══════════════════════════════════════════════════════════════
#  Co-Pilot
# ══════════════════════════════════════════════════════════════
@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    from copilot import get_copilot_response
    data = request.get_json() or {}
    q    = (data.get("query") or data.get("question", "")).strip()
    if not q:
        return jsonify({"error": "query required"}), 400
    r = get_copilot_response(q)
    t = r.get("text", "")
    return jsonify({
        "answer": t, "text": t, "mode": r.get("mode", "?"),
        "reports_analysed": r.get("snapshot", {}).get("total", 0),
        "audio": r.get("audio")
    })


@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    if not re.match(r'^copilot_\d+\.mp3$', filename):
        return jsonify({"error": "invalid"}), 400
    p = f"/tmp/{filename}"
    return send_file(p, mimetype="audio/mpeg") if os.path.exists(p) else (jsonify({"error": "not found"}), 404)


# ══════════════════════════════════════════════════════════════
#  Health check + root USSD fallback
# ══════════════════════════════════════════════════════════════
@app.route("/", methods=["GET", "POST"])
def health():
    if request.method == "POST" and request.form.get("sessionId"):
        return ussd()
    return jsonify({
        "status":    "Kingolik running",
        "ussd":      "*789*1990#",
        "flash":     YOUR_NUMBER,
        "sms":       f"SMS any keyword to {YOUR_NUMBER}",
        "dashboard": "/dashboard",
        "analytics": "/analytics",
        "config":    "/test/at-config",
    }), 200


# ══════════════════════════════════════════════════════════════
#  WebSocket events
# ══════════════════════════════════════════════════════════════
@socketio.on("connect")
def on_connect():
    Log.ok("WS connected")


@socketio.on("disconnect")
def on_disconnect():
    Log.info("WS disconnected")


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    Log.section("Kingolik NGO Voice Bridge")
    Log.ok(f"port={port}  number={YOUR_NUMBER}  alert={ALERT_PHONE or 'disabled'}")
    Log.ok(f"base={BASE_URL}")
    Log.ok(f"whitelist={sorted(ALLOWED) if ALLOWED else 'open'}")
    Log.divider()
    socketio.run(app, host="0.0.0.0", port=port, debug=False)