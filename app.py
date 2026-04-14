import eventlet
eventlet.monkey_patch()

from flask_socketio import SocketIO
from flask import Flask, request, make_response, jsonify, send_file
import africastalking
import os, re, json, datetime, threading, logging, time
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

from dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)

from sync_queue import start_sync_worker
start_sync_worker()

# ══════════════════════════════════════════════════════════════
#  Africa's Talking init
# ══════════════════════════════════════════════════════════════
AT_USERNAME = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY  = os.getenv("AT_API_KEY", "")
YOUR_NUMBER = os.getenv("AT_VIRTUAL_NUMBER", "+254711082547")
BASE_URL    = os.getenv("BASE_URL", "https://kingo-lik-engine.onrender.com")

voice_service = None
sms_service   = None

if AT_API_KEY:
    africastalking.initialize(username=AT_USERNAME, api_key=AT_API_KEY)
    voice_service = africastalking.Voice
    sms_service   = africastalking.SMS
    print(f"[AT] Initialized  username={AT_USERNAME}  number={YOUR_NUMBER}")
else:
    print("[AT WARNING] No AT_API_KEY — voice disabled")


# ══════════════════════════════════════════════════════════════
#  Terminal logger
# ══════════════════════════════════════════════════════════════
class Log:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"
    CYAN  = "\033[36m"; WHITE  = "\033[37m"; BLUE = "\033[34m"

    @staticmethod
    def _ts(): return datetime.datetime.now().strftime("%H:%M:%S")

    @classmethod
    def info(cls, m): print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.CYAN}INFO {cls.RESET} {m}")
    @classmethod
    def ok(cls, m):   print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.GREEN}{cls.BOLD} OK  {cls.RESET} {m}")
    @classmethod
    def warn(cls, m): print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.YELLOW}WARN {cls.RESET} {m}")
    @classmethod
    def error(cls, m):print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.RED}ERR  {cls.RESET} {m}")

    @classmethod
    def ussd(cls, session_id, phone, text):
        short = (session_id or "?")[-8:]
        print(
            f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.BOLD}USSD{cls.RESET}"
            f"  {cls.WHITE}{phone}{cls.RESET}"
            f"  [{cls.DIM}...{short}{cls.RESET}]"
            f"  text={cls.BOLD}'{text}'{cls.RESET}"
        )

    @classmethod
    def section(cls, t):
        print(f"\n{cls.DIM}┌── {t} {'─'*max(0,46-len(t))}{cls.RESET}")

    @classmethod
    def divider(cls):
        print(f"{cls.DIM}{'─'*54}{cls.RESET}")


# ══════════════════════════════════════════════════════════════
#  Whitelist + rate limiter
# ══════════════════════════════════════════════════════════════
_raw = os.getenv("ALLOWED_PHONES", "")
ALLOWED_PHONES = set(p.strip() for p in _raw.split(",") if p.strip()) if _raw else set()
print(f"[SEC] Whitelist: {len(ALLOWED_PHONES)} numbers" if ALLOWED_PHONES else "[SEC] Sandbox mode — all phones accepted")

_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()
RATE_LIMIT  = 999   # effectively unlimited during testing — set to 3 for production
RATE_WINDOW = 3600

def _is_rate_limited(phone: str) -> bool:
    now = time.time()
    with _rate_lock:
        _rate_store[phone] = [t for t in _rate_store[phone] if now - t < RATE_WINDOW]
        if len(_rate_store[phone]) >= RATE_LIMIT:
            return True
        _rate_store[phone].append(now)
        return False


# ══════════════════════════════════════════════════════════════
#  XML builders
# ══════════════════════════════════════════════════════════════
def xml_reject():
    """Reject inbound call. Caller charged KES 0.00."""
    r = make_response(
        '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>', 200
    )
    r.headers["Content-Type"] = "application/xml"
    return r


def xml_greeting(session_id: str) -> str:
    """
    Greeting + Record XML served to the user when the outbound call connects.
    callbackUrl points to /voice/save which processes the recording.
    """
    save_url = f"{BASE_URL}/voice/save?session_id={session_id}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="woman" playBeep="false">
        Habari, karibu King'olik.
        Simu hii ni bure kwako.
        Tafadhali sema ujumbe wako baada ya mlio.
        Bonyeza nyota ukimaliza.
    </Say>
    <Record
        finishOnKey="*"
        maxLength="120"
        trimSilence="true"
        playBeep="true"
        callbackUrl="{save_url}"
    />
</Response>"""


# ══════════════════════════════════════════════════════════════
#  Werkzeug noise filter
# ══════════════════════════════════════════════════════════════
class _PF(logging.Filter):
    def filter(self, r):
        m = r.getMessage()
        return ('"/api/sessions' not in m and '"/api/audio/' not in m
                and '"/favicon' not in m and '"/api/analytics' not in m)

logging.getLogger("werkzeug").addFilter(_PF())

@app.before_request
def _log_req():
    silent = {'/api/sessions', '/dashboard', '/analytics', '/favicon.ico'}
    if request.path in silent or request.path.startswith('/api/audio/'):
        return
    args = {k: v for k, v in request.args.items() if k != 't'}
    Log.info(f"{request.method} {request.path}" + (f"  {args}" if args else ""))


# ══════════════════════════════════════════════════════════════
#  USSD
# ══════════════════════════════════════════════════════════════
@app.route("/ussd", methods=["POST"])
def ussd():
    from database import save_session

    session_id   = request.form.get("sessionId", "")
    phone_number = request.form.get("phoneNumber", "")
    text         = request.form.get("text", "").strip()

    Log.ussd(session_id, phone_number, text)

    if ALLOWED_PHONES and phone_number not in ALLOWED_PHONES:
        return _text_resp("END System restricted to authorised personnel only.")

    parts = text.split("*") if text else []

    if text == "":
        return _text_resp(
            "CON Karibu King'olik.\n"
            "1. Afya / Health\n"
            "2. Chakula / Food\n"
            "3. Usalama / Security\n"
            "4. Ingineo / Other"
        )

    elif text in ("1","2","3"):
        labels = {"1":"Afya/Health","2":"Chakula/Food","3":"Usalama/Security"}
        return _text_resp(
            f"CON {labels[text]}\n"
            "Tutakupigia ndani ya sekunde 10.\n"
            "We will call you in 10 seconds.\n"
            "1. Thibitisha / Confirm\n"
            "0. Ghairi / Cancel"
        )

    elif text == "4":
        return _text_resp(
            "CON Andika shida kwa ufupi:\n"
            "Type your issue briefly:"
        )

    elif text.startswith("4*") and len(parts) >= 2:
        free_text = "*".join(parts[1:])
        from database import save_session as _sv, save_translation
        _sv({"session_id":session_id,"phone":phone_number,"menu_choice":"4",
             "timestamp":datetime.datetime.utcnow().isoformat(),"status":"text_report"})
        save_translation(session_id, {
            "transcript": free_text, "detected_language": "sw",
            "translation": f"[Text report] {free_text}",
            "urgent_keywords": _kw_scan(free_text),
            "confidence": "high", "engine": "ussd_text"
        })
        Log.ok(f"Text report saved  [{phone_number}]  '{free_text[:40]}'")
        return _text_resp("END Ahsante. Tumepokea ripoti yako.\nThank you. Report received.")

    elif text.endswith("*1"):
        if _is_rate_limited(phone_number):
            return _text_resp("END Limit imefikiwa. Jaribu baadaye.\nLimit reached. Try in 60 min.")

        menu_choice = parts[0] if parts else "1"
        save_session({
            "session_id": session_id, "phone": phone_number,
            "menu_choice": menu_choice,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "status": "pending_call"
        })
        Log.ok(f"USSD confirm  [{phone_number}]  menu={menu_choice}")
        threading.Thread(
            target=trigger_voice_callback,
            args=(phone_number, session_id), daemon=True
        ).start()
        return _text_resp(
            "END Asante! Tutakupigia sekunde 10.\n"
            "Thank you! Calling you in 10 seconds.\n"
            "Simu ni BURE / Call is FREE."
        )

    elif text.endswith("*0"):
        return _text_resp("END Umeghairi. / Cancelled.\nDial *789*1990# anytime.")

    else:
        return _text_resp("END Chaguo batili.\nInvalid. Dial *789*1990#")


def _text_resp(body: str):
    r = make_response(body, 200)
    r.headers["Content-Type"] = "text/plain"
    return r


def _kw_scan(text: str) -> list:
    KW = ["maji","moto","damu","jeraha","vita","chakula","njaa","hatari",
          "msaada","mgonjwa","fire","water","food","help","sick","danger"]
    t = text.lower()
    return [k for k in KW if k in t]


# ══════════════════════════════════════════════════════════════
#  Outbound call trigger
# ══════════════════════════════════════════════════════════════
def trigger_voice_callback(phone_number: str, session_id: str):
    """
    Waits 5 seconds then makes outbound call from YOUR_NUMBER to phone_number.
    When answered, AT will POST to /voice/answer with callSessionState=Active.
    We serve the greeting XML there.
    """
    Log.info(f"Callback in 5s  [...{session_id[-8:]}]  →  {phone_number}")
    time.sleep(5)

    if not voice_service:
        Log.warn("No voice_service — test audio fallback")
        _fallback(phone_number, session_id)
        return

    # Store session_id so /voice/answer can look it up by phone
    _pending_calls[phone_number] = session_id
    Log.info(f"Registered pending  {phone_number} → {session_id[-8:]}")

    try:
        resp    = voice_service.call(callFrom=YOUR_NUMBER, callTo=[phone_number])
        entries = resp.get("entries", [])
        status  = entries[0].get("status","?") if entries else "no_entries"
        Log.ok(f"Call placed  status={status}  phone={phone_number}")

        if status not in ("Queued","Ringing","Success"):
            Log.warn(f"Unexpected status '{status}' — test fallback")
            _fallback(phone_number, session_id)
    except Exception as e:
        Log.error(f"voice.call failed: {e}")
        _fallback(phone_number, session_id)


# Map phone → session_id so /voice/answer knows which session this call is for
_pending_calls: dict = {}


def _fallback(phone: str, session_id: str):
    wav = _pick_wav()
    if not wav:
        Log.warn(f"No WAV for fallback [{session_id[-8:]}]")
        return
    Log.info(f"Test audio fallback  [{session_id[-8:]}]  {os.path.basename(wav)}")
    threading.Thread(
        target=_translate, args=(session_id, wav, phone), daemon=True
    ).start()


def _translate(session_id: str, path: str, phone: str):
    time.sleep(1)
    from translator import process_recording
    process_recording(session_id, path, phone)


# ══════════════════════════════════════════════════════════════
#  /voice/flash — inbound flash entry point
#
#  Set this as the "Voice callback URL" in AT dashboard for +254711082547.
#  When someone calls your number, AT posts here.
#  We REJECT the call (KES 0.00 to caller) then call them back.
# ══════════════════════════════════════════════════════════════
@app.route("/voice/flash", methods=["POST", "GET"])
def voice_flash():
    caller = (
        request.form.get("callerNumber") or
        request.values.get("callerNumber") or ""
    ).strip()
    Log.info(f"Flash/inbound  caller={caller}")

    if not caller:
        return xml_reject()
    if ALLOWED_PHONES and caller not in ALLOWED_PHONES:
        Log.warn(f"Blocked flash: {caller}")
        return xml_reject()
    if _is_rate_limited(caller):
        Log.warn(f"Rate limit flash: {caller}")
        return xml_reject()

    session_id = f"flash_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
    from database import save_session
    save_session({
        "session_id": session_id, "phone": caller, "menu_choice": "flash",
        "timestamp": datetime.datetime.utcnow().isoformat(), "status": "pending_call"
    })
    Log.ok(f"Flash received from {caller} — callback in 5s")
    threading.Thread(
        target=trigger_voice_callback, args=(caller, session_id), daemon=True
    ).start()
    return xml_reject()


# ══════════════════════════════════════════════════════════════
#  /voice/answer — outbound call answered
#
#  Set this as the "Action URL" when making outbound calls.
#  AT posts here when the person picks up — we serve the greeting.
#
#  HOW IT WORKS:
#  voice.call(callFrom=YOUR_NUMBER, callTo=[phone]) triggers AT to call phone.
#  When phone is answered, AT sends POST to /voice/answer with:
#    - callerNumber = the phone we called
#    - callSessionState = "Active"
#  We look up the session_id from _pending_calls[callerNumber]
#  and serve the greeting + record XML.
# ══════════════════════════════════════════════════════════════
@app.route("/voice/answer", methods=["POST", "GET"])
def voice_answer():
    """Served when our OUTBOUND call is answered. Returns greeting + Record XML."""
    # AT may send callerNumber as the number we called, or as YOUR_NUMBER
    # directional info varies — we check both
    caller      = (request.form.get("callerNumber") or
                   request.values.get("callerNumber") or "").strip()
    destination = (request.form.get("destinationNumber") or
                   request.values.get("destinationNumber") or "").strip()
    call_state  = (request.form.get("callSessionState") or
                   request.values.get("callSessionState") or "").lower()
    session_id  = (request.args.get("session_id") or
                   request.values.get("sessionId") or "")

    Log.info(
        f"Voice answer  caller={caller}  dest={destination}"
        f"  state={call_state}  session=[...{(session_id or '?')[-8:]}]"
    )

    # Find the session_id — either passed in URL or looked up from pending calls
    if not session_id:
        # The person we called is either in caller or destination
        user_phone = destination if destination != YOUR_NUMBER else caller
        session_id = _pending_calls.pop(user_phone, None) or \
                     _pending_calls.pop(caller, None) or \
                     f"answer_{datetime.datetime.utcnow().strftime('%H%M%S')}"

    Log.ok(f"Serving greeting  session=[...{session_id[-8:]}]")

    xml  = xml_greeting(session_id)
    resp = make_response(xml, 200)
    resp.headers["Content-Type"] = "application/xml"
    return resp


# ══════════════════════════════════════════════════════════════
#  /voice/save — recording callback
#
#  AT posts here after the user speaks and presses *.
#  We download and translate the audio.
# ══════════════════════════════════════════════════════════════
@app.route("/voice/save", methods=["POST", "GET"])
def voice_save():
    from database import update_call_record
    from translator import process_recording

    session_id    = (request.args.get("session_id") or
                     request.values.get("sessionId") or "")
    recording_url = (request.form.get("recordingUrl") or
                     request.values.get("recordingUrl") or "")
    duration      = (request.form.get("durationInSeconds") or
                     request.values.get("durationInSeconds") or "0")

    short = (session_id or "?")[-8:]
    Log.info(f"Recording  [...{short}]  dur={duration}s  has_url={bool(recording_url)}")

    if recording_url:
        if session_id:
            update_call_record(session_id, recording_url, duration)
        s_id = session_id or f"rec_{datetime.datetime.utcnow().strftime('%H%M%S')}"
        Log.ok(f"Translating  [...{s_id[-8:]}]  {recording_url[:60]}")
        threading.Thread(
            target=process_recording, args=(s_id, recording_url), daemon=True
        ).start()
    else:
        Log.warn(f"No recordingUrl in /voice/save  session=[{short}]")

    return "", 200  # AT expects empty 200


# ══════════════════════════════════════════════════════════════
#  /sms — Please-Call-Me / SMS trigger
# ══════════════════════════════════════════════════════════════
@app.route("/sms", methods=["POST", "GET"])
def sms_callback():
    sender = (request.values.get("from") or request.form.get("from", "")).strip()
    text   = (request.values.get("text") or request.form.get("text", "")).strip()
    Log.info(f"SMS  from={sender}  text='{text[:60]}'")
    if not sender:
        return "", 200
    if ALLOWED_PHONES and sender not in ALLOWED_PHONES:
        return "", 200
    if _is_rate_limited(sender):
        return "", 200
    session_id = f"pcm_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
    from database import save_session
    save_session({"session_id":session_id,"phone":sender,"menu_choice":"pcm",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
    Log.ok(f"PCM from {sender} — calling back in 5s")
    threading.Thread(target=trigger_voice_callback, args=(sender,session_id), daemon=True).start()
    return "", 200


# ══════════════════════════════════════════════════════════════
#  WAV picker
# ══════════════════════════════════════════════════════════════
def _pick_wav():
    d = os.path.join(os.getcwd(), "recordings")
    try:
        files = [
            os.path.join(d, f) for f in os.listdir(d)
            if f.endswith(".wav") and "_clean" not in f and "_raw_clean" not in f
            and not any(f.startswith(p) for p in ("ATUid_","local_test_","url_test_"))
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
    sid  = request.args.get("session_id", f"local_{datetime.datetime.utcnow().strftime('%H%M%S')}")
    path = (os.path.join(os.getcwd(), request.args.get("file")) if request.args.get("file")
            else _pick_wav())
    if not path or not os.path.exists(path):
        return jsonify({"error":"No WAV found"}), 404
    threading.Thread(target=process_recording, args=(sid, path), daemon=True).start()
    return jsonify({"status":"processing","session_id":sid,"file":os.path.basename(path)}), 200


@app.route("/test/file/<filename>")
def test_file(filename):
    from translator import process_recording
    sid  = f"local_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    path = os.path.join(os.getcwd(), "recordings", filename)
    if not os.path.exists(path):
        return jsonify({"error":f"{filename} not found",
                        "available":[f for f in os.listdir("recordings")
                                     if f.endswith(".wav") and "_clean" not in f]}), 404
    threading.Thread(target=process_recording, args=(sid, path), daemon=True).start()
    return jsonify({"status":"processing","file":filename,"session_id":sid}), 200


@app.route("/test/call/<path:phone>")
def test_call(phone):
    """
    Manually trigger a full end-to-end call to any number.
    /test/call/+254714137554
    Your phone rings in ~5 seconds. Answer, speak, press *, see translation.
    """
    if not voice_service:
        return jsonify({"error":"AT voice not initialized — check AT_API_KEY on Render"}), 500
    sid = f"testcall_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({"session_id":sid,"phone":phone,"menu_choice":"test",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
    threading.Thread(target=trigger_voice_callback, args=(phone, sid), daemon=True).start()
    Log.ok(f"Test call to {phone}  session={sid}")
    return jsonify({
        "status":  "calling now",
        "phone":   phone,
        "session": sid,
        "note":    "Your phone should ring in ~5 seconds. Answer, speak, press *."
    }), 200


@app.route("/test/url")
def test_url():
    from translator import process_recording
    url = request.args.get("url","")
    if not url:
        return jsonify({"error":"url param required"}), 400
    sid = f"url_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    threading.Thread(target=process_recording, args=(sid, url), daemon=True).start()
    return jsonify({"status":"processing","session_id":sid}), 200


# ── Co-Pilot ──────────────────────────────────────────────────
@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    from copilot import get_copilot_response
    data  = request.get_json() or {}
    query = (data.get("query") or data.get("question","")).strip()
    if not query:
        return jsonify({"error":"query required"}), 400
    r    = get_copilot_response(query)
    text = r.get("text","")
    return jsonify({"answer":text,"text":text,"mode":r.get("mode","?"),
                    "reports_analysed":r.get("snapshot",{}).get("total",0),
                    "audio":r.get("audio")})


@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    if not re.match(r'^copilot_\d+\.mp3$', filename):
        return jsonify({"error":"invalid"}), 400
    path = f"/tmp/{filename}"
    return send_file(path, mimetype="audio/mpeg") if os.path.exists(path) \
           else (jsonify({"error":"not found"}), 404)


# ── Health ────────────────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
def health():
    if request.method == "POST" and request.form.get("sessionId"):
        return ussd()
    return jsonify({
        "status":          "Kingolik running",
        "ussd":            "*789*1990#",
        "flash":           YOUR_NUMBER,
        "dashboard":       "/dashboard",
        "analytics":       "/analytics",
        "test_call":       "/test/call/+YOUR_NUMBER",
        "at_setup": {
            "ussd_callback":    f"{BASE_URL}/ussd",
            "voice_flash_url":  f"{BASE_URL}/voice/flash",
            "voice_answer_url": f"{BASE_URL}/voice/answer",
            "sms_callback":     f"{BASE_URL}/sms",
        }
    }), 200


# ── WebSocket ─────────────────────────────────────────────────
@socketio.on("connect")
def on_connect(): Log.ok("WS connected")

@socketio.on("disconnect")
def on_disconnect(): Log.info("WS disconnected")


# ── Entry ─────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    Log.section("Kingolik NGO Voice Bridge")
    Log.ok(f"port={port}  number={YOUR_NUMBER}")
    Log.ok(f"Flash URL  → {BASE_URL}/voice/flash")
    Log.ok(f"Answer URL → {BASE_URL}/voice/answer")
    Log.divider()
    socketio.run(app, host="0.0.0.0", port=port, debug=False)