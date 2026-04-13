# app.py
from flask_socketio import SocketIO
from flask import Flask, request, make_response, jsonify
import africastalking
import os, json, datetime, threading, logging, time
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
# ── Register dashboard blueprint ──────────────────────────────
from dashboard import dashboard_bp
app.register_blueprint(dashboard_bp)

# Start background sync worker
from sync_queue import start_sync_worker
start_sync_worker()

# ── Africa's Talking init ─────────────────────────────────────
africastalking.initialize(
    username="sandbox",
    api_key=os.environ.get("AT_API_KEY")
)


# ══════════════════════════════════════════════════════════════
#  Structured terminal logger
#  Log.info / .ok / .warn / .error / .ussd / .translate / .audio
#  Timestamped so `grep` and `tail -f` work cleanly in production.
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
    def translate(cls, session_id, engine, score=None):
        short     = session_id[-8:] if session_id else "?"
        score_str = f"  score={score}" if score else ""
        tag = (f"{cls.BLUE}CLOUD{cls.RESET}" if engine == "cloud"
               else f"{cls.YELLOW}LOCAL{cls.RESET}")
        print(
            f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.BOLD}XLAT {cls.RESET}"
            f"  {tag}  {cls.DIM}[...{short}]{cls.RESET}{score_str}"
        )

    @classmethod
    def audio(cls, session_id, filename):
        short = session_id[-8:] if session_id else "?"
        print(
            f"{cls.DIM}{cls._ts()}{cls.RESET}  {cls.GREEN}AUDIO{cls.RESET}"
            f"  {cls.DIM}[...{short}]{cls.RESET}  -> {filename}"
        )

    @classmethod
    def section(cls, title):
        bar = "─" * max(0, 48 - len(title))
        print(f"\n{cls.DIM}┌── {title} {bar}{cls.RESET}")

    @classmethod
    def divider(cls):
        print(f"{cls.DIM}{'─' * 54}{cls.RESET}")



# ══════════════════════════════════════════════════════════════
#  Rate limiter — anti-fraud, anti-prank-call protection
#  Max 3 USSD reports per phone number per hour.
#  Protects AT balance and Gemini API credits.
# ══════════════════════════════════════════════════════════════
import time as _time
from collections import defaultdict

_rate_store   = defaultdict(list)   # phone -> [timestamps]
_rate_lock    = threading.Lock()
RATE_LIMIT    = 3       # max reports per window
RATE_WINDOW   = 3600    # 1 hour in seconds


def _is_rate_limited(phone: str) -> bool:
    """Returns True if this phone has exceeded the limit."""
    now = _time.time()
    with _rate_lock:
        timestamps = _rate_store[phone]
        # Drop timestamps outside the window
        _rate_store[phone] = [t for t in timestamps if now - t < RATE_WINDOW]
        if len(_rate_store[phone]) >= RATE_LIMIT:
            return True
        _rate_store[phone].append(now)
        return False


# ── Silence Flask werkzeug HTTP log for high-frequency routes ─
class _PollFilter(logging.Filter):
    """Drop GET /api/sessions, /api/audio/*, /favicon.ico lines.
    Everything else (USSD, test routes, errors) still prints."""
    def filter(self, record):
        m = record.getMessage()
        return (
            '"/api/sessions'  not in m and
            '"/api/audio/'    not in m and
            '"/favicon.ico'   not in m
        )

logging.getLogger("werkzeug").addFilter(_PollFilter())


# ── Log every meaningful incoming request ────────────────────
@app.before_request
def log_request():
    silent = {'/api/sessions', '/dashboard', '/favicon.ico'}
    if request.path in silent or request.path.startswith('/api/audio/'):
        return
    meaningful_args = {k: v for k, v in request.args.items() if k != 't'}
    extra = f"  args={meaningful_args}" if meaningful_args else ""
    Log.info(f"{request.method} {request.path}{extra}")


# ══════════════════════════════════════════════════════════════
#  USSD handler
#  FIX: .endswith() instead of exact match — handles handsets
#  that accumulate navigation history in the text string.
# ══════════════════════════════════════════════════════════════
@app.route("/ussd", methods=["POST"])
def ussd():
    from database import save_session

    session_id   = request.form.get("sessionId")
    phone_number = request.form.get("phoneNumber")
    text         = request.form.get("text", "").strip()

    Log.ussd(session_id, phone_number, text)

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
        # ── Rate limit check ──────────────────────────────────
        if _is_rate_limited(phone_number):
            remaining = RATE_WINDOW // 60
            Log.warn(f"Rate limit hit: {phone_number}")
            response = (
                "END Limit imefikiwa. Jaribu baadaye.\n"
                f"(Limit reached. Try again in {remaining} min.)"
            )
            resp = make_response(response, 200)
            resp.headers["Content-Type"] = "text/plain"
            return resp

        menu_choice = text.split("*")[0]
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
#  Voice callback trigger
#  FIX (race condition): snapshot the source WAV at dispatch
#  time — NOT after sleeping — so two concurrent sessions cannot
#  steal each other's audio file.
# ══════════════════════════════════════════════════════════════
def _pick_source_wav():
    """Return the absolute path of the newest non-derived WAV, or None."""
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
    # Snapshot NOW before the 2s sleep — this is the race-condition fix
    file_path = _pick_source_wav()

    if not file_path:
        Log.warn(f"No source WAV at dispatch — session [...{session_id[-8:]}] will stall")
        return

    Log.info(f"Callback queued  [...{session_id[-8:]}]  file={os.path.basename(file_path)}")

    def process():
        time.sleep(2)  # grace period for file to finish writing
        size = os.path.getsize(file_path)
        Log.info(f"Processing start [...{session_id[-8:]}]  {size} bytes")
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
    <Record
        finishOnKey="#"
        maxLength="120"
        trimSilence="true"
        playBeep="true"
        callbackUrl="{base_url}/voice/record?session_id={session_id}"
    />
</Response>"""
    resp = make_response(xml, 200)
    resp.headers["Content-Type"] = "application/xml"
    return resp


# ── TEST: local wav file ──────────────────────────────────────
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
    threading.Thread(
        target=process_recording, args=(session_id, abs_path), daemon=True
    ).start()

    return jsonify({
        "status": "processing started",
        "session_id": session_id,
        "file": os.path.basename(abs_path)
    }), 200


@app.route("/test/latest", methods=["GET"])
def test_latest():
    from translator import process_recording
    from database import _load

    sessions = _load()
    pending  = [
        s for s in sessions.values()
        if s.get("status") == "pending_call" and s.get("phone") != "local-test"
    ]
    if not pending:
        return jsonify({
            "error": "No pending sessions found",
            "tip":   "Dial *384*67660# on the simulator first"
        }), 404

    latest     = sorted(pending, key=lambda x: x.get("timestamp", ""), reverse=True)[0]
    session_id = latest["session_id"]
    phone      = latest["phone"]

    file_path = os.path.join(os.getcwd(), "recordings", "test_audio.wav")
    if not os.path.exists(file_path):
        return jsonify({"error": "recordings/test_audio.wav not found"}), 404

    Log.info(f"Test latest  [...{session_id[-8:]}]  phone={phone}")
    threading.Thread(
        target=process_recording, args=(session_id, file_path), daemon=True
    ).start()

    return jsonify({
        "status":     "processing started",
        "session_id": session_id,
        "phone":      phone,
        "tip":        "Watch dashboard — pending card will flip to URGENT"
    }), 200


# ── TEST: remote URL ──────────────────────────────────────────
@app.route("/test/url", methods=["GET"])
def test_url():
    from translator import process_recording
    audio_url  = request.args.get(
        "url",
        "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
    )
    session_id = f"url_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    Log.info(f"Test URL  session={session_id}  url={audio_url[:60]}")
    threading.Thread(
        target=process_recording, args=(session_id, audio_url), daemon=True
    ).start()
    return jsonify({
        "status": "processing started",
        "session_id": session_id,
        "url": audio_url
    }), 200


# ── TEST: specific file by name ───────────────────────────────
@app.route("/test/file/<filename>", methods=["GET"])
def test_file(filename):
    from translator import process_recording
    session_id = f"local_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    file_path  = os.path.join(os.getcwd(), "recordings", filename)

    if not os.path.exists(file_path):
        available = [
            f for f in os.listdir("recordings")
            if f.endswith(".wav") and "_clean" not in f
        ]
        return jsonify({"error": f"{filename} not found", "available": available}), 404

    Log.info(f"Test file  session={session_id[-8:]}  file={filename}")
    threading.Thread(
        target=process_recording, args=(session_id, file_path), daemon=True
    ).start()
    return jsonify({
        "status": "processing started",
        "file": filename,
        "session_id": session_id
    }), 200


# ── Co-Pilot API ─────────────────────────────────────────────
@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    from copilot import get_copilot_response
    data  = request.get_json()
    query = (data.get("query") or data.get("question","")).strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    result = get_copilot_response(query)
    return jsonify(result)


@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    import re
    # Sanitise filename — only allow safe characters
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
        Log.info("AT posted to / — redirecting to USSD handler")
        return ussd()
    return jsonify({
        "status":    "Kingolik running",
        "service":   "*384*67660#",
        "dashboard": "/dashboard"
    }), 200


# ── WebSocket events ──────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    Log.ok("Dashboard connected via WebSocket")

@socketio.on("disconnect")
def on_disconnect():
    Log.info("Dashboard disconnected")


if __name__ == "__main__":
    Log.section("Kingolik NGO Voice Bridge")
    Log.ok("Flask starting  port=5000")
    Log.divider()
socketio.run(app, debug=True, port=5000)