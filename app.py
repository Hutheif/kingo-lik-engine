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
#  Terminal logger
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
#  Phone whitelist
# ══════════════════════════════════════════════════════════════
_raw_whitelist = os.getenv("ALLOWED_PHONES", "")
ALLOWED_PHONES = set(
    p.strip() for p in _raw_whitelist.split(",") if p.strip()
) if _raw_whitelist else set()

if ALLOWED_PHONES:
    print(f"[SECURITY] Whitelist active: {len(ALLOWED_PHONES)} numbers")
else:
    print("[SECURITY] No whitelist — sandbox mode")


# ══════════════════════════════════════════════════════════════
#  Rate limiter
# ══════════════════════════════════════════════════════════════
_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()
RATE_LIMIT  = 999
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
#  XML helpers
# ══════════════════════════════════════════════════════════════
def _reject_xml():
    """Rejects inbound call — user charged KES 0.00."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Reject/>
</Response>"""
    resp = make_response(xml, 200)
    resp.headers["Content-Type"] = "application/xml"
    return resp


def _record_xml(session_id: str, base_url: str) -> str:
    """XML served when outbound call is answered. Greets + records."""
    callback = f"{base_url}/voice/save?session_id={session_id}"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="woman" playBeep="false">
        Habari. Karibu King-olik.
        Simu hii inarekodiwa kwa usalama wa data yako.
        Tafadhali sema ujumbe wako baada ya mlio, kisha bonyeza nyota ukimaliza.
        Hello. Welcome to King apostrophe olik.
        This call is recorded for your data security.
        Please state your message after the beep, then press star when finished.
    </Say>
    <Record
        finishOnKey="*"
        maxLength="120"
        trimSilence="true"
        playBeep="true"
        callbackUrl="{callback}"
    />
</Response>"""


# ══════════════════════════════════════════════════════════════
#  Werkzeug poll filter
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
#  USSD handler — with Ingineo free-text option
#
#  Menu tree:
#    ""          Main menu (1 Afya / 2 Chakula / 3 Usalama / 4 Ingineo)
#    "1"/"2"/"3" Confirm callback sub-menu
#    "4"         Free text prompt
#    "4*<text>"  Save text report
#    "*1"        Confirm callback
#    "*0"        Cancel
# ══════════════════════════════════════════════════════════════
@app.route("/ussd", methods=["POST"])
def ussd():
    from database import save_session

    session_id   = request.form.get("sessionId")
    phone_number = request.form.get("phoneNumber")
    text         = request.form.get("text", "").strip()

    Log.ussd(session_id, phone_number, text)

    if ALLOWED_PHONES and phone_number not in ALLOWED_PHONES:
        Log.warn(f"Blocked: {phone_number}")
        resp = make_response(
            "END Mfumo huu umewekwa kwa wafanyakazi walioidhinishwa tu.\n"
            "This system is restricted to authorised field personnel only.", 200
        )
        resp.headers["Content-Type"] = "text/plain"
        return resp

    parts = text.split("*") if text else []

    if text == "":
        response = (
            "CON Karibu King'olik.\n"
            "Chagua huduma / Select service:\n"
            "1. Afya (Health)\n"
            "2. Chakula (Food)\n"
            "3. Usalama (Security)\n"
            "4. Ingineo (Other — describe issue)"
        )

    elif text in ("1", "2", "3"):
        labels = {"1": "Afya / Health", "2": "Chakula / Food", "3": "Usalama / Security"}
        response = (
            f"CON {labels[text]}:\n"
            "Tutakupigia simu ndani ya sekunde 10.\n"
            "We will call you back in 10 seconds.\n"
            "1. Thibitisha / Confirm\n"
            "0. Ghairi / Cancel"
        )

    elif text == "4":
        response = (
            "CON Tafadhali andika shida yako kwa ufupi:\n"
            "Please describe your issue briefly:"
        )

    elif text.startswith("4*") and len(parts) >= 2:
        free_text = "*".join(parts[1:])
        session_data = {
            "session_id": session_id,
            "phone":       phone_number,
            "menu_choice": "4_text",
            "timestamp":   datetime.datetime.utcnow().isoformat(),
            "status":      "text_report",
            "source_wav":  ""
        }
        from database import save_session as _save, save_translation
        _save(session_data)
        save_translation(session_id, {
            "transcript":        free_text,
            "detected_language": "sw",
            "translation":       f"[Text report] {free_text}",
            "urgent_keywords":   _scan_text_keywords(free_text),
            "confidence":        "high",
            "engine":            "ussd_text"
        })
        Log.ok(f"Text report  [...{session_id[-8:]}]  '{free_text[:40]}'")
        response = (
            "END Ahsante. Tumepokea ripoti yako.\n"
            "Thank you. We have received your report."
        )

    elif text.endswith("*1"):
        if _is_rate_limited(phone_number):
            Log.warn(f"Rate limit: {phone_number}")
            resp = make_response(
                "END Limit imefikiwa. Jaribu baadaye.\n"
                "Limit reached. Try again in 60 minutes.", 200
            )
            resp.headers["Content-Type"] = "text/plain"
            return resp

        menu_choice = parts[0] if parts else "1"
        save_session({
            "session_id": session_id,
            "phone":       phone_number,
            "menu_choice": menu_choice,
            "timestamp":   datetime.datetime.utcnow().isoformat(),
            "status":      "pending_call"
        })
        Log.ok(f"Session saved  [...{session_id[-8:]}]  phone={phone_number}")
        threading.Thread(
            target=trigger_voice_callback,
            args=(phone_number, session_id),
            daemon=True
        ).start()
        response = (
            "END Asante! Tutakupigia simu ndani ya sekunde 10.\n"
            "Thank you! We will call you in 10 seconds.\n"
            "Simu hii ni BURE / This call is FREE."
        )

    elif text.endswith("*0"):
        response = "END Umeghairi. Piga tena: *789*1990#\nCancelled. Dial: *789*1990#"

    else:
        response = "END Chaguo batili. Jaribu tena: *789*1990#\nInvalid option."

    resp = make_response(response, 200)
    resp.headers["Content-Type"] = "text/plain"
    return resp


def _scan_text_keywords(text: str) -> list:
    URGENT = ["maji","moto","damu","jeraha","vita","shambulio","chakula","njaa",
              "hatari","msaada","mgonjwa","fire","water","food","help","sick",
              "violence","attack","blood","injury","danger","emergency"]
    text_lower = text.lower()
    return [kw for kw in URGENT if kw in text_lower]


# ══════════════════════════════════════════════════════════════
#  Voice callback trigger
# ══════════════════════════════════════════════════════════════
def trigger_voice_callback(phone_number: str, session_id: str):
    Log.info(f"Callback in 5s  [...{session_id[-8:]}]  phone={phone_number}")
    time.sleep(5)
    if not voice_service:
        Log.warn("No voice_service — test audio fallback")
        _fallback_to_test_audio(phone_number, session_id)
        return
    try:
        response = voice_service.call(callFrom=YOUR_NUMBER, callTo=[phone_number])
        entries  = response.get("entries", [])
        if entries:
            status = entries[0].get("status", "?")
            Log.ok(f"Call initiated  status={status}  phone={phone_number}")
            if status not in ("Queued", "Ringing", "Success"):
                _fallback_to_test_audio(phone_number, session_id)
        else:
            _fallback_to_test_audio(phone_number, session_id)
    except Exception as e:
        Log.error(f"Call failed: {e}")
        _fallback_to_test_audio(phone_number, session_id)


def _fallback_to_test_audio(phone_number: str, session_id: str):
    file_path = _pick_source_wav()
    if not file_path:
        Log.warn(f"No WAV for fallback  [...{session_id[-8:]}]")
        return
    Log.info(f"Test audio fallback  [...{session_id[-8:]}]")
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
#  /voice — Flash-to-callback entry point
#
#  THE FIX for call hang-up:
#  AT does NOT reliably send "direction" field on outbound callbacks.
#  Instead read "callSessionState":
#    "Active"    = our outbound call was ANSWERED — serve greeting XML
#    anything else = inbound flash — REJECT and callback
# ══════════════════════════════════════════════════════════════
@app.route("/voice", methods=["POST", "GET"])
def voice_callback():
    caller_number = (
        request.form.get("callerNumber") or
        request.values.get("callerNumber") or ""
    )
    call_state = (
        request.form.get("callSessionState") or
        request.values.get("callSessionState") or ""
    ).lower()

    session_id = (
        request.args.get("session_id") or
        request.form.get("sessionId") or
        request.values.get("sessionId") or
        f"flash_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    )

    Log.info(
        f"Voice event  caller={caller_number}"
        f"  state={call_state or 'none'}"
        f"  session=[...{session_id[-8:]}]"
    )

    if not caller_number:
        return _reject_xml()

    if ALLOWED_PHONES and caller_number not in ALLOWED_PHONES:
        Log.warn(f"Blocked voice: {caller_number}")
        return _reject_xml()

    # ── Outbound call answered → serve greeting + record ─────
    if call_state == "active":
        Log.ok(f"Outbound ANSWERED by {caller_number} — serving greeting")
        base_url = os.environ.get("BASE_URL", "https://kingo-lik-engine.onrender.com")
        xml = _record_xml(session_id, base_url)
        resp = make_response(xml, 200)
        resp.headers["Content-Type"] = "application/xml"
        return resp

    # ── Inbound flash → reject (KES 0.00) + callback ─────────
    if _is_rate_limited(caller_number):
        Log.warn(f"Rate limit flash: {caller_number}")
        return _reject_xml()

    from database import save_session
    save_session({
        "session_id": session_id,
        "phone":       caller_number,
        "menu_choice": "flash",
        "timestamp":   datetime.datetime.utcnow().isoformat(),
        "status":      "pending_call"
    })
    Log.ok(f"Flash from {caller_number} — callback in 5s")
    threading.Thread(
        target=trigger_voice_callback,
        args=(caller_number, session_id),
        daemon=True
    ).start()
    return _reject_xml()


# ══════════════════════════════════════════════════════════════
#  /voice/save — recording callback (called AFTER user speaks)
#  Only processes the audio. Never serves a greeting here.
# ══════════════════════════════════════════════════════════════
@app.route("/voice/save", methods=["POST", "GET"])
def voice_save():
    from database import update_call_record
    from translator import process_recording

    session_id    = request.args.get("session_id") or request.values.get("sessionId", "")
    recording_url = request.form.get("recordingUrl") or request.values.get("recordingUrl", "")
    duration      = request.form.get("durationInSeconds") or request.values.get("durationInSeconds", "0")

    short = session_id[-8:] if session_id else "?"
    Log.info(f"Recording callback  [...{short}]  has_url={bool(recording_url)}  {duration}s")

    if recording_url:
        if session_id:
            update_call_record(session_id, recording_url, duration)
        s_id = session_id or f"at_{datetime.datetime.utcnow().strftime('%H%M%S')}"
        Log.ok(f"Processing  [...{s_id[-8:]}]  {recording_url[:50]}")
        threading.Thread(
            target=process_recording,
            args=(s_id, recording_url),
            daemon=True
        ).start()
    else:
        Log.warn("No recordingUrl in /voice/save")

    return "", 200  # AT expects empty 200 here


# ══════════════════════════════════════════════════════════════
#  /sms — Please-Call-Me trigger
# ══════════════════════════════════════════════════════════════
@app.route("/sms", methods=["POST", "GET"])
def sms_callback():
    sender = request.values.get("from") or request.form.get("from", "")
    text   = (request.values.get("text") or request.form.get("text", "")).strip()
    Log.info(f"SMS from {sender}: '{text[:60]}'")

    if not sender:
        return "", 200
    if ALLOWED_PHONES and sender not in ALLOWED_PHONES:
        return "", 200
    if _is_rate_limited(sender):
        return "", 200

    session_id = f"sms_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({
        "session_id": session_id, "phone": sender,
        "menu_choice": "sms_pcm",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "status": "pending_call"
    })
    Log.ok(f"PCM trigger — calling back {sender} in 5s")
    threading.Thread(
        target=trigger_voice_callback,
        args=(sender, session_id),
        daemon=True
    ).start()
    return "", 200


# ══════════════════════════════════════════════════════════════
#  WAV picker
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
        Log.error(f"WAV scan: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  Test routes
# ══════════════════════════════════════════════════════════════
@app.route("/test/local", methods=["GET"])
def test_local():
    from translator import process_recording
    session_id = request.args.get(
        "session_id", f"local_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    )
    abs_path = (
        os.path.join(os.getcwd(), request.args.get("file"))
        if request.args.get("file") else _pick_source_wav()
    )
    if not abs_path or not os.path.exists(abs_path):
        return jsonify({"error": "WAV not found"}), 404
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
    threading.Thread(target=process_recording, args=(session_id, file_path), daemon=True).start()
    return jsonify({"status": "processing started", "file": filename,
                    "session_id": session_id}), 200


@app.route("/test/call/<phone>", methods=["GET"])
def test_call(phone):
    if not voice_service:
        return jsonify({"error": "AT voice not initialized"}), 500
    session_id = f"testcall_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({
        "session_id": session_id, "phone": phone,
        "menu_choice": "test_call",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "status": "pending_call"
    })
    threading.Thread(
        target=trigger_voice_callback, args=(phone, session_id), daemon=True
    ).start()
    return jsonify({"status": "call initiated", "phone": phone,
                    "note": "Phone should ring in ~10 seconds"}), 200


@app.route("/test/url", methods=["GET"])
def test_url():
    from translator import process_recording
    audio_url = request.args.get("url", "")
    if not audio_url:
        return jsonify({"error": "url required"}), 400
    session_id = f"url_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
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
        return jsonify({"error": "No pending sessions"}), 404
    latest   = sorted(pending, key=lambda x: x.get("timestamp",""), reverse=True)[0]
    s_id     = latest["session_id"]
    phone    = latest["phone"]
    wav      = _pick_source_wav()
    if not wav:
        return jsonify({"error": "No WAV"}), 404
    threading.Thread(target=process_recording, args=(s_id, wav, phone), daemon=True).start()
    return jsonify({"status": "processing started", "session_id": s_id}), 200


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
        return ussd()
    return jsonify({
        "status":    "Kingolik running",
        "service":   "*789*1990#",
        "flash":     YOUR_NUMBER,
        "dashboard": "/dashboard",
        "analytics": "/analytics"
    }), 200


# ── WebSocket ─────────────────────────────────────────────────
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
    Log.ok(f"Port: {port}  |  Number: {YOUR_NUMBER}")
    Log.divider()
    socketio.run(app, host="0.0.0.0", port=port, debug=False)