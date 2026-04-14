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

# ── Register dashboard blueprint ──────────────────────────────
from dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)

# ── Start background sync worker ──────────────────────────────
from sync_queue import start_sync_worker
start_sync_worker()

# ══════════════════════════════════════════════════════════════
#  Africa's Talking init — ALL AT objects created here
# ══════════════════════════════════════════════════════════════
AT_USERNAME   = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY    = os.getenv("AT_API_KEY", "")
YOUR_NUMBER   = os.getenv("AT_VIRTUAL_NUMBER", "+254711082547")

voice_service = None
sms_service   = None

if AT_API_KEY:
    africastalking.initialize(username=AT_USERNAME, api_key=AT_API_KEY)
    voice_service = africastalking.Voice
    sms_service   = africastalking.SMS
    print(f"[AT] Initialized  username={AT_USERNAME}  number={YOUR_NUMBER}")
else:
    print("[AT WARNING] No AT_API_KEY — voice callbacks disabled")


# ══════════════════════════════════════════════════════════════
#  Structured terminal logger  (must be before routes that use Log)
# ══════════════════════════════════════════════════════════════
class Log:
    RESET  = "\033[0m";  BOLD   = "\033[1m";  DIM    = "\033[2m"
    GREEN  = "\033[32m"; YELLOW = "\033[33m"; RED    = "\033[31m"
    CYAN   = "\033[36m"; WHITE  = "\033[37m"; BLUE   = "\033[34m"

    @staticmethod
    def _ts():
        return datetime.datetime.now().strftime("%H:%M:%S")

    @classmethod
    def info(cls, msg):
        print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.CYAN}INFO {cls.RESET} {msg}")

    @classmethod
    def ok(cls, msg):
        print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.GREEN}{cls.BOLD} OK  {cls.RESET} {msg}")

    @classmethod
    def warn(cls, msg):
        print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.YELLOW}WARN {cls.RESET} {msg}")

    @classmethod
    def error(cls, msg):
        print(f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.RED}ERR  {cls.RESET} {msg}")

    @classmethod
    def ussd(cls, session_id, phone, text):
        short = session_id[-8:] if session_id else "?"
        print(
            f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.BOLD}USSD {cls.RESET}"
            f"  {cls.WHITE}{phone}{cls.RESET}"
            f"  {cls.DIM}[...{short}]{cls.RESET}"
            f"  input={cls.BOLD}'{text}'{cls.RESET}"
        )

    @classmethod
    def section(cls, title):
        bar = "─" * max(0, 48 - len(title))
        print(f"\n{cls.DIM}┌── {title} {bar}{cls.RESET}")

    @classmethod
    def divider(cls):
        print(f"{cls.DIM}{'─' * 54}{cls.RESET}")


# ══════════════════════════════════════════════════════════════
#  Phone whitelist — cost protection
#  Set ALLOWED_PHONES=+254700000001,+254700000002 in .env
#  Leave blank to allow all phones (sandbox/testing mode)
# ══════════════════════════════════════════════════════════════
_raw_whitelist = os.getenv("ALLOWED_PHONES", "")
ALLOWED_PHONES = set(
    p.strip() for p in _raw_whitelist.split(",") if p.strip()
) if _raw_whitelist else set()

if ALLOWED_PHONES:
    print(f"[SECURITY] Whitelist active: {len(ALLOWED_PHONES)} authorised numbers")
else:
    print("[SECURITY] No whitelist — all phones accepted (sandbox mode)")


# ══════════════════════════════════════════════════════════════
#  Rate limiter — max 3 reports per phone per hour
# ══════════════════════════════════════════════════════════════
_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()
RATE_LIMIT  = 
RATE_WINDOW = 


def _is_rate_limited(phone: str) -> bool:
    now = time.time()
    with _rate_lock:
        _rate_store[phone] = [t for t in _rate_store[phone] if now - t < RATE_WINDOW]
        if len(_rate_store[phone]) >= RATE_LIMIT:
            return True
        _rate_store[phone].append(now)
        return False


# ══════════════════════════════════════════════════════════════
#  Silence Flask poll noise
# ══════════════════════════════════════════════════════════════
class _PollFilter(logging.Filter):
    def filter(self, record):
        m = record.getMessage()
        return (
            '"/api/sessions'  not in m and
            '"/api/audio/'    not in m and
            '"/favicon.ico'   not in m and
            '"/api/analytics' not in m
        )

logging.getLogger("werkzeug").addFilter(_PollFilter())


@app.before_request
def log_request():
    silent = {'/api/sessions', '/dashboard', '/analytics', '/favicon.ico'}
    if request.path in silent or request.path.startswith('/api/audio/'):
        return
    meaningful_args = {k: v for k, v in request.args.items() if k != 't'}
    extra = f"  args={meaningful_args}" if meaningful_args else ""
    Log.info(f"{request.method} {request.path}{extra}")


# ══════════════════════════════════════════════════════════════
#  USSD handler
# ══════════════════════════════════════════════════════════════
@app.route("/ussd", methods=["POST"])
def ussd():
    from database import save_session

    session_id   = request.form.get("sessionId")
    phone_number = request.form.get("phoneNumber")
    text         = request.form.get("text", "").strip()

    Log.ussd(session_id, phone_number, text)

    if ALLOWED_PHONES and phone_number not in ALLOWED_PHONES:
        Log.warn(f"Blocked non-whitelisted: {phone_number}")
        resp = make_response(
            "END This system is restricted to authorised field personnel only.", 200
        )
        resp.headers["Content-Type"] = "text/plain"
        return resp

    if text == "":
        response = (
            "CON Karibu / Welcome\n"
            "1. Report an issue\n"
            "2. Request assistance\n"
            "3. Leave a message"
        )

    elif text in ("1", "2", "3"):
        labels = {
            "1": "Report an issue",
            "2": "Request assistance",
            "3": "Leave a voice message"
        }
        response = f"CON {labels[text]}:\n1. Confirm callback\n0. Cancel"

    elif text.endswith("*1"):
        if _is_rate_limited(phone_number):
            Log.warn(f"Rate limit hit: {phone_number}")
            resp = make_response(
                "END Limit imefikiwa. Jaribu baadaye.\n(Limit reached. Try again in 60 min.)",
                200
            )
            resp.headers["Content-Type"] = "text/plain"
            return resp

        menu_choice  = text.split("*")[0]
        session_data = {
            "session_id": session_id,
            "phone":       phone_number,
            "menu_choice": menu_choice,
            "timestamp":   datetime.datetime.utcnow().isoformat(),
            "status":      "pending_call"
        }
        save_session(session_data)
        Log.ok(f"Session saved  [...{session_id[-8:]}]  phone={phone_number}")

        # Trigger callback in background — non-blocking
        threading.Thread(
            target=trigger_voice_callback,
            args=(phone_number, session_id),
            daemon=True
        ).start()

        response = (
            "END Asante! / Thank you!\n"
            "We will call you in 10 seconds.\n"
            "This call is FREE."
        )

    elif text.endswith("*0"):
        response = "END Cancelled. Dial *789*1990# to try again."

    else:
        response = "END Invalid option. Please try again: *789*1990#"

    resp = make_response(response, 200)
    resp.headers["Content-Type"] = "text/plain"
    return resp


# ══════════════════════════════════════════════════════════════
#  Voice callback trigger — calls the user back after USSD
#  Uses Africa's Talking Voice API to make outbound call
#  The call plays voice/record XML to record the user's message
# ══════════════════════════════════════════════════════════════
def trigger_voice_callback(phone_number: str, session_id: str):
    """
    Called in background thread after USSD confirm.
    Waits 10 seconds then initiates outbound call from
    YOUR_NUMBER to phone_number via AT Voice API.
    """
    Log.info(f"Callback scheduled  [...{session_id[-8:]}]  phone={phone_number}  wait=10s")
    time.sleep(10)  # ← reduced from 30s to 10s

    if not voice_service:
        Log.warn("voice_service not initialized — falling back to test audio")
        _fallback_to_test_audio(phone_number, session_id)
        return

    base_url = os.environ.get("BASE_URL", "https://kingo-lik-engine.onrender.com")

    try:
        # AT Voice API — make outbound call
        # callFrom = your virtual number
        # callTo   = the user's real phone
        response = voice_service.call(
            callFrom=YOUR_NUMBER,
            callTo=[phone_number]
        )
        Log.ok(f"Voice call initiated  [...{session_id[-8:]}]  response={response}")

        # Check if call was accepted
        entries = response.get("entries", [])
        if entries:
            status = entries[0].get("status", "?")
            Log.info(f"Call status: {status}  phone={phone_number}")
            if status not in ("Queued", "Ringing", "Success"):
                Log.warn(f"Unexpected status '{status}' — using test audio fallback")
                _fallback_to_test_audio(phone_number, session_id)
        else:
            Log.warn(f"No entries in voice response — fallback to test audio")
            _fallback_to_test_audio(phone_number, session_id)

    except Exception as e:
        Log.error(f"Voice call failed: {e} — falling back to test audio")
        _fallback_to_test_audio(phone_number, session_id)


def _fallback_to_test_audio(phone_number: str, session_id: str):
    """
    When voice call fails or no AT production account,
    process the test audio file directly so the dashboard
    still shows a result.
    """
    file_path = _pick_source_wav()
    if not file_path:
        Log.warn(f"No source WAV for fallback  [...{session_id[-8:]}]")
        return
    Log.info(f"Test audio fallback  [...{session_id[-8:]}]  file={os.path.basename(file_path)}")
    threading.Thread(
        target=_run_translation,
        args=(session_id, file_path, phone_number),
        daemon=True
    ).start()


def _run_translation(session_id: str, file_path: str, phone_number: str):
    time.sleep(1)
    from translator import process_recording
    process_recording(session_id, file_path, phone_number)


# ══════════════════════════════════════════════════════════════
#  Flash-to-callback (missed call trigger)
#  User flashes your number → system calls them back FREE
#  Endpoint: set as "Voice callback URL" in AT dashboard
# ══════════════════════════════════════════════════════════════
@app.route("/voice", methods=["POST", "GET"])
def voice_callback():
    """
    AT calls this when someone calls +254711082547.
    We REJECT the incoming call (caller charged KES 0.00)
    then call them back via trigger_voice_callback.
    """
    caller_number = (
        request.form.get("callerNumber") or
        request.values.get("callerNumber") or
        request.args.get("callerNumber")
    )
    is_active = (
        request.form.get("isActive") or
        request.values.get("isActive", "1")
    )
    session_id = (
        request.form.get("sessionId") or
        request.values.get("sessionId") or
        f"flash_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    )

    Log.info(f"Voice callback  caller={caller_number}  isActive={is_active}")

    if not caller_number:
        # No caller info — return silence XML
        return _reject_xml()

    if ALLOWED_PHONES and caller_number not in ALLOWED_PHONES:
        Log.warn(f"Blocked voice from {caller_number}")
        return _reject_xml()

    if is_active == "1" and _is_rate_limited(caller_number):
        Log.warn(f"Rate limit: {caller_number}")
        return _reject_xml()

    if is_active == "1":
        # Save a pending session for this flash call
        from database import save_session
        save_session({
            "session_id": session_id,
            "phone":       caller_number,
            "menu_choice": "flash",
            "timestamp":   datetime.datetime.utcnow().isoformat(),
            "status":      "pending_call"
        })
        Log.ok(f"Flash received from {caller_number} — initiating callback in 10s")

        threading.Thread(
            target=trigger_voice_callback,
            args=(caller_number, session_id),
            daemon=True
        ).start()

    # Always reject the incoming call — user is charged KES 0.00
    return _reject_xml()


def _reject_xml():
    xml = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>'
    resp = make_response(xml, 200)
    resp.headers["Content-Type"] = "application/xml"
    return resp


# ══════════════════════════════════════════════════════════════
#  Voice recording webhook
#  AT calls this after the user records their message
#  This is the URL in your <Record callbackUrl="..."/>
# ══════════════════════════════════════════════════════════════
@app.route("/voice/record", methods=["POST", "GET"])
def voice_record():
    from database import update_call_record
    from translator import process_recording

    session_id    = request.args.get("session_id") or request.values.get("sessionId")
    recording_url = request.form.get("recordingUrl") or request.values.get("recordingUrl")
    duration      = request.form.get("durationInSeconds") or request.values.get("durationInSeconds")

    Log.info(f"Voice record  session={session_id}  has_url={bool(recording_url)}")

    if recording_url:
        if session_id:
            update_call_record(session_id, recording_url, duration)
        short = (session_id or "?")[-8:]
        Log.ok(f"Recording received  [...{short}]  {duration}s  url={recording_url[:50]}")
        s_id = session_id or f"at_call_{datetime.datetime.utcnow().strftime('%H%M%S')}"
        threading.Thread(
            target=process_recording,
            args=(s_id, recording_url),
            daemon=True
        ).start()

    base_url = os.environ.get("BASE_URL", "https://kingo-lik-engine.onrender.com")

    # This XML is served when AT first connects the outbound call
    # It greets the user and records their message
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="woman" playBeep="true">
        Habari. Karibu. Mfumo wa habari wa King-olik.
        Ombi lako la kusaidia limepokelewa.
        Tafadhali sema ujumbe wako baada ya mlio.
        Bonyeza nyota uishamaliza.
    </Say>
    <Record
        finishOnKey="*"
        maxLength="120"
        trimSilence="true"
        playBeep="true"
        callbackUrl="{base_url}/voice/record?session_id={session_id or 'unknown'}"
    />
</Response>"""
    resp = make_response(xml, 200)
    resp.headers["Content-Type"] = "application/xml"
    return resp


# ══════════════════════════════════════════════════════════════
#  SMS / Please-Call-Me trigger
#  Set as SMS callback in AT dashboard
# ══════════════════════════════════════════════════════════════
@app.route("/sms", methods=["POST", "GET"])
def sms_callback():
    sender = request.values.get("from") or request.form.get("from")
    text   = request.values.get("text") or request.form.get("text", "")

    Log.info(f"SMS from {sender}: '{text[:50]}'")

    if not sender:
        return "", 200

    if ALLOWED_PHONES and sender not in ALLOWED_PHONES:
        Log.warn(f"SMS from unauthorized: {sender}")
        return "", 200

    if _is_rate_limited(sender):
        Log.warn(f"Rate limit for SMS trigger: {sender}")
        return "", 200

    session_id = f"sms_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({
        "session_id": session_id,
        "phone":       sender,
        "menu_choice": "sms",
        "timestamp":   datetime.datetime.utcnow().isoformat(),
        "status":      "pending_call"
    })

    threading.Thread(
        target=trigger_voice_callback,
        args=(sender, session_id),
        daemon=True
    ).start()

    Log.ok(f"SMS trigger — calling back {sender} in 10s")
    return "", 200


# ══════════════════════════════════════════════════════════════
#  WAV picker — finds newest source file in recordings/
# ══════════════════════════════════════════════════════════════
def _pick_source_wav():
    recordings_dir = os.path.join(os.getcwd(), "recordings")
    try:
        candidates = [
            os.path.join(recordings_dir, f)
            for f in os.listdir(recordings_dir)
            if f.endswith(".wav")
            and "_clean"     not in f
            and "_raw_clean" not in f
            and not f.startswith("ATUid_")
            and not f.startswith("local_test_")
            and not f.startswith("url_test_")
        ]
        return max(candidates, key=os.path.getmtime) if candidates else None
    except Exception as e:
        Log.error(f"WAV scan failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  Test routes — for development without spending AT credits
# ══════════════════════════════════════════════════════════════
@app.route("/test/local", methods=["GET"])
def test_local():
    from translator import process_recording
    session_id = request.args.get(
        "session_id",
        f"local_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    )
    if request.args.get("file"):
        abs_path = os.path.join(os.getcwd(), request.args.get("file"))
    else:
        abs_path = _pick_source_wav()
        if not abs_path:
            return jsonify({"error": "No source WAV files in recordings/"}), 404
    if not os.path.exists(abs_path):
        return jsonify({"error": f"File not found: {abs_path}"}), 404
    Log.info(f"Test local  session={session_id[-8:]}  file={os.path.basename(abs_path)}")
    threading.Thread(target=process_recording, args=(session_id, abs_path), daemon=True).start()
    return jsonify({
        "status": "processing started",
        "session_id": session_id,
        "file": os.path.basename(abs_path)
    }), 200


@app.route("/test/file/<filename>", methods=["GET"])
def test_file(filename):
    from translator import process_recording
    session_id = f"local_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    file_path  = os.path.join(os.getcwd(), "recordings", filename)
    if not os.path.exists(file_path):
        available = [f for f in os.listdir("recordings")
                     if f.endswith(".wav") and "_clean" not in f]
        return jsonify({"error": f"{filename} not found", "available": available}), 404
    Log.info(f"Test file  session={session_id[-8:]}  file={filename}")
    threading.Thread(target=process_recording, args=(session_id, file_path), daemon=True).start()
    return jsonify({"status": "processing started", "file": filename, "session_id": session_id}), 200


@app.route("/test/call/<phone>", methods=["GET"])
def test_call(phone):
    """
    Trigger a real outbound call to any number.
    GET /test/call/+254714137554
    Use this to verify AT voice works end-to-end.
    """
    if not voice_service:
        return jsonify({"error": "voice_service not initialized — check AT_API_KEY"}), 500
    session_id = f"testcall_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({
        "session_id": session_id,
        "phone": phone,
        "menu_choice": "test_call",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "status": "pending_call"
    })
    threading.Thread(
        target=trigger_voice_callback,
        args=(phone, session_id),
        daemon=True
    ).start()
    Log.info(f"Manual test call to {phone}  session={session_id}")
    return jsonify({
        "status": "call initiated",
        "phone": phone,
        "session_id": session_id,
        "wait": "10 seconds then your phone should ring"
    }), 200


@app.route("/test/url", methods=["GET"])
def test_url():
    from translator import process_recording
    audio_url  = request.args.get("url", "")
    if not audio_url:
        return jsonify({"error": "url parameter required"}), 400
    session_id = f"url_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    Log.info(f"Test URL  session={session_id}")
    threading.Thread(target=process_recording, args=(session_id, audio_url), daemon=True).start()
    return jsonify({"status": "processing started", "session_id": session_id}), 200


@app.route("/test/latest", methods=["GET"])
def test_latest():
    from translator import process_recording
    from database import _load
    sessions = _load()
    pending  = [s for s in sessions.values()
                if s.get("status") == "pending_call" and s.get("phone") != "local-test"]
    if not pending:
        return jsonify({"error": "No pending sessions", "tip": "Dial *789*1990# first"}), 404
    latest     = sorted(pending, key=lambda x: x.get("timestamp",""), reverse=True)[0]
    session_id = latest["session_id"]
    phone      = latest["phone"]
    file_path  = _pick_source_wav()
    if not file_path:
        return jsonify({"error": "No WAV file available"}), 404
    Log.info(f"Test latest  [...{session_id[-8:]}]  phone={phone}")
    threading.Thread(target=process_recording, args=(session_id, file_path, phone), daemon=True).start()
    return jsonify({"status": "processing started", "session_id": session_id, "phone": phone}), 200


# ── Co-Pilot API ──────────────────────────────────────────────
@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    from copilot import get_copilot_response
    data  = request.get_json() or {}
    query = (data.get("query") or data.get("question", "")).strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    result = get_copilot_response(query)
    text   = result.get("text", "")
    return jsonify({
        "answer":           text,
        "text":             text,
        "mode":             result.get("mode", "unknown"),
        "reports_analysed": result.get("snapshot", {}).get("total", 0),
        "audio":            result.get("audio")
    })


@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    if not re.match(r'^copilot_\d+\.mp3$', filename):
        return jsonify({"error": "invalid"}), 400
    path = f"/tmp/{filename}"
    if os.path.exists(path):
        return send_file(path, mimetype="audio/mpeg")
    return jsonify({"error": "not found"}), 404


# ── Health check ──────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def health():
    if request.method == "POST" and request.form.get("sessionId"):
        Log.info("AT hit / — redirecting to USSD")
        return ussd()
    return jsonify({
        "status":       "Kingolik running",
        "service":      "*789*1990#",
        "dashboard":    "/dashboard",
        "analytics":    "/analytics",
        "voice_bridge": YOUR_NUMBER,
        "test_call":    "/test/call/+YOUR_PHONE"
    }), 200


# ── WebSocket events ──────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    Log.ok("Dashboard connected via WebSocket")


@socketio.on("disconnect")
def on_disconnect():
    Log.info("Dashboard disconnected")


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    Log.section("Kingolik NGO Voice Bridge")
    Log.ok(f"Starting on port {port}")
    Log.ok(f"Virtual number: {YOUR_NUMBER}")
    Log.divider()
    socketio.run(app, host="0.0.0.0", port=port, debug=False)