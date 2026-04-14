# app.py — King'olik NGO Voice Bridge
from flask_socketio import SocketIO
from flask import Flask, request, make_response, jsonify, send_file
import africastalking
import os, re, json, datetime, threading, logging, time
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

# ── Africa's Talking init ─────────────────────────────────────
AT_USERNAME = os.getenv("AT_USERNAME", "sandbox")
AT_API_KEY  = os.getenv("AT_API_KEY", "")

if AT_API_KEY:
    africastalking.initialize(username=AT_USERNAME, api_key=AT_API_KEY)
    print(f"[AT] Initialized  username={AT_USERNAME}")
else:
    print("[AT WARNING] No AT_API_KEY — running without Africa's Talking")
    #To get the "Flash-to-Callback"This route handles the logic where the system "sees" the call to +254711082547, rejects it to save the user money, and then calls them back
@app.route("/voice", methods=['POST'])
def voice_callback():
    # 1. Get the caller's info
    caller_number = request.values.get("callerNumber")
    is_active     = request.values.get("isActive") 

    # 2. Safety Check (Whitelist & Rate Limiter)
    if ALLOWED_PHONES and caller_number not in ALLOWED_PHONES:
        Log.warn(f"Blocked unauthorized flash from {caller_number}")
        return '<Response><Reject/></Response>'

    if is_active == '1' and _is_rate_limited(caller_number):
        Log.warn(f"Rate limit exceeded for {caller_number}")
        return '<Response><Reject/></Response>'

    # 3. The "Flash" Logic
    if is_active == '1':
        Log.info(f"Flash received from {caller_number}. Initiating AI Callback...")

        if voice_service:
            try:
                # This triggers the call FROM your number TO the user
                voice_service.call("+254711082547", [caller_number])
                Log.ok(f"Callback command sent for {caller_number}")
            except Exception as e:
                Log.error(f"Voice API failed: {str(e)}")

        # 4. REJECT the incoming call so the user is charged KES 0.00
        return '<Response><Reject/></Response>'

    return ""


# ── Phone whitelist — cost protection ─────────────────────────
# Set ALLOWED_PHONES=+254700000001,+254700000002 in .env
# Leave blank to allow all phones (sandbox testing mode)
_raw_whitelist = os.getenv("ALLOWED_PHONES", "")
ALLOWED_PHONES = set(
    p.strip() for p in _raw_whitelist.split(",") if p.strip()
) if _raw_whitelist else set()

if ALLOWED_PHONES:
    print(f"[SECURITY] Whitelist active: {len(ALLOWED_PHONES)} authorised numbers")
else:
    print("[SECURITY] No whitelist — all phones accepted (sandbox mode)")


# ══════════════════════════════════════════════════════════════
#  Structured terminal logger
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
#  Rate limiter — max 3 reports per phone per hour
# ══════════════════════════════════════════════════════════════
import time as _time
from collections import defaultdict

_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()
RATE_LIMIT  = 3
RATE_WINDOW = 3600


def _is_rate_limited(phone: str) -> bool:
    now = _time.time()
    with _rate_lock:
        _rate_store[phone] = [t for t in _rate_store[phone] if now - t < RATE_WINDOW]
        if len(_rate_store[phone]) >= RATE_LIMIT:
            return True
        _rate_store[phone].append(now)
        return False


# ── Silence Flask poll noise ──────────────────────────────────
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

    # ── Whitelist check ───────────────────────────────────────
    if ALLOWED_PHONES and phone_number not in ALLOWED_PHONES:
        Log.warn(f"Blocked non-whitelisted number: {phone_number}")
        resp = make_response(
            "END This humanitarian system is restricted to authorised field personnel only.",
            200
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
        labels = {"1": "Report an issue", "2": "Request assistance", "3": "Leave a voice message"}
        response = f"CON {labels[text]}:\n1. Confirm callback\n0. Cancel"

    elif text.endswith("*1"):
        # Rate limit check
        if _is_rate_limited(phone_number):
            Log.warn(f"Rate limit hit: {phone_number}")
            resp = make_response(
                f"END Limit imefikiwa. Jaribu baadaye.\n(Limit reached. Try again in 60 min.)",
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

        threading.Thread(
            target=trigger_callback,
            args=(phone_number, session_id),
            daemon=True
        ).start()

        response = (
            "END Asante! / Thank you!\n"
            "We will call you in 30 seconds.\n"
            "This call is FREE."
        )

    elif text.endswith("*0"):
        response = "END Cancelled. Dial *384*67660# to try again."

    else:
        response = "END Invalid option. Please try again: *384*67660#"

    resp = make_response(response, 200)
    resp.headers["Content-Type"] = "text/plain"
    return resp


# ══════════════════════════════════════════════════════════════
#  WAV picker + callback trigger
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


def trigger_callback(phone_number, session_id):
    file_path = _pick_source_wav()
    if not file_path:
        Log.warn(f"No source WAV — [...{session_id[-8:]}] stalled")
        return
    Log.info(f"Callback queued  [...{session_id[-8:]}]  file={os.path.basename(file_path)}")

    def process():
        time.sleep(2)
        Log.info(f"Processing start [...{session_id[-8:]}]  {os.path.getsize(file_path)} bytes")
        from translator import process_recording
        process_recording(session_id, file_path, phone_number)

    threading.Thread(target=process, daemon=True).start()


# ── Voice recording webhook ───────────────────────────────────
@app.route("/voice/record", methods=["POST", "GET"])
def voice_record():
    from database import update_call_record
    from translator import process_recording

    session_id    = request.args.get("session_id")
    recording_url = request.form.get("recordingUrl")
    duration      = request.form.get("durationInSeconds")

    if recording_url:
        update_call_record(session_id, recording_url, duration)
        short = session_id[-8:] if session_id else "?"
        Log.ok(f"Recording received  [...{short}]  {duration}s")
        threading.Thread(
            target=process_recording,
            args=(session_id, recording_url),
            daemon=True
        ).start()

    base_url = os.environ.get("BASE_URL", "http://127.0.0.1:5000")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="woman" playBeep="true">
        Habari. Karibu. This call is free. Please speak your message after the beep. Press hash when done.
    </Say>
    <Record finishOnKey="#" maxLength="120" trimSilence="true" playBeep="true"
            callbackUrl="{base_url}/voice/record?session_id={session_id}"/>
</Response>"""
    resp = make_response(xml, 200)
    resp.headers["Content-Type"] = "application/xml"
    return resp


# ── Test routes ───────────────────────────────────────────────
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
    return jsonify({"status": "processing started", "session_id": session_id,
                    "file": os.path.basename(abs_path)}), 200


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
    return jsonify({"status": "processing started", "file": filename,
                    "session_id": session_id}), 200


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
        return jsonify({"error": "No pending sessions", "tip": "Dial *384*67660# first"}), 404
    latest     = sorted(pending, key=lambda x: x.get("timestamp",""), reverse=True)[0]
    session_id = latest["session_id"]
    phone      = latest["phone"]
    file_path  = os.path.join(os.getcwd(), "recordings", "test_audio.wav")
    if not os.path.exists(file_path):
        return jsonify({"error": "recordings/test_audio.wav not found"}), 404
    Log.info(f"Test latest  [...{session_id[-8:]}]  phone={phone}")
    threading.Thread(target=process_recording, args=(session_id, file_path), daemon=True).start()
    return jsonify({"status": "processing started", "session_id": session_id, "phone": phone}), 200


# ── Co-Pilot API ──────────────────────────────────────────────
@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    from copilot import get_copilot_response
    data  = request.get_json()
    query = (data.get("query") or data.get("question", "")).strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    result = get_copilot_response(query)
    return jsonify(result)


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
        Log.info("AT posted to / — redirecting to USSD")
        return ussd()
    return jsonify({
        "status":    "Kingolik running",
        "service":   "*384*67660#",
        "dashboard": "/dashboard",
        "analytics": "/analytics"
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
    Log.divider()
    socketio.run(app, host="0.0.0.0", port=port, debug=False)