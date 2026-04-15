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
    print(f"[AT] OK  username={AT_USERNAME}  number={YOUR_NUMBER}")
else:
    print("[AT] WARNING — no AT_API_KEY, voice disabled")


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
#  Whitelist + rate limiter
# ══════════════════════════════════════════════════════════════
_raw_wl = os.getenv("ALLOWED_PHONES","")
ALLOWED = set(p.strip() for p in _raw_wl.split(",") if p.strip()) if _raw_wl else set()
print(f"[SEC] {len(ALLOWED)} whitelisted numbers" if ALLOWED else "[SEC] All phones accepted")

_rs = defaultdict(list); _rl = threading.Lock()
RLIMIT=999; RWIN=3600   # set RLIMIT=3 for production

def _limited(phone:str)->bool:
    now=time.time()
    with _rl:
        _rs[phone]=[t for t in _rs[phone] if now-t<RWIN]
        if len(_rs[phone])>=RLIMIT: return True
        _rs[phone].append(now); return False


# ══════════════════════════════════════════════════════════════
#  _pending_calls: phone → session_id
#  Populated when we place an outbound call.
#  Read inside /voice/answer to identify which session is ringing.
# ══════════════════════════════════════════════════════════════
_pending: dict = {}


# ══════════════════════════════════════════════════════════════
#  XML helpers
# ══════════════════════════════════════════════════════════════
def _xml(body:str):
    r=make_response(body,200); r.headers["Content-Type"]="application/xml"; return r

def reject_xml():
    return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>')

def greeting_xml(session_id:str):
    cb=f"{BASE_URL}/voice/save?session_id={session_id}"
    return _xml(f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="woman" playBeep="false">
    Habari, karibu King apostrophe olik.
    Simu hii ni bure kwako.
    Tafadhali sema ujumbe wako baada ya mlio, kisha bonyeza nyota ukimaliza.
  </Say>
  <Record finishOnKey="*" maxLength="120" trimSilence="true"
          playBeep="true" callbackUrl="{cb}"/>
</Response>""")


# ══════════════════════════════════════════════════════════════
#  Werkzeug noise filter
# ══════════════════════════════════════════════════════════════
class _PF(logging.Filter):
    def filter(self,r):
        m=r.getMessage()
        return not any(x in m for x in ['"/api/sessions','"/api/audio/','"/favicon','"/api/analytics'])
logging.getLogger("werkzeug").addFilter(_PF())

@app.before_request
def _lr():
    skip={'/api/sessions','/dashboard','/analytics','/favicon.ico'}
    if request.path in skip or request.path.startswith('/api/audio/'): return
    a={k:v for k,v in request.args.items() if k!='t'}
    Log.info(f"{request.method} {request.path}"+(f" {a}" if a else ""))


# ══════════════════════════════════════════════════════════════
#  USSD
# ══════════════════════════════════════════════════════════════
@app.route("/ussd", methods=["POST"])
def ussd():
    from database import save_session
    sid   = request.form.get("sessionId","")
    phone = request.form.get("phoneNumber","")
    text  = request.form.get("text","").strip()
    Log.ussd(sid, phone, text)

    if ALLOWED and phone not in ALLOWED:
        return _tr("END System restricted to authorised personnel only.")

    parts = text.split("*") if text else []

    if text=="":
        return _tr("CON Karibu King'olik.\n1. Afya/Health\n2. Chakula/Food\n3. Usalama/Security\n4. Ingineo/Other")

    if text in("1","2","3"):
        lbl={"1":"Afya/Health","2":"Chakula/Food","3":"Usalama/Security"}
        return _tr(f"CON {lbl[text]}\nTutakupigia ndani ya sekunde 10.\nWe will call you in 10 seconds.\n1. Thibitisha/Confirm\n0. Ghairi/Cancel")

    if text=="4":
        return _tr("CON Andika shida kwa ufupi:\nType your issue briefly:")

    if text.startswith("4*") and len(parts)>=2:
        ftxt="*".join(parts[1:])
        from database import save_session as sv, save_translation
        sv({"session_id":sid,"phone":phone,"menu_choice":"4",
            "timestamp":datetime.datetime.utcnow().isoformat(),"status":"text_report"})
        save_translation(sid,{"transcript":ftxt,"detected_language":"sw",
            "translation":f"[Text] {ftxt}","urgent_keywords":_kws(ftxt),
            "confidence":"high","engine":"ussd_text"})
        Log.ok(f"Text report [{phone}] '{ftxt[:40]}'")
        return _tr("END Ahsante. Tumepokea ripoti yako.\nThank you. Report received.")

    if text.endswith("*1"):
        if _limited(phone):
            return _tr("END Limit imefikiwa. Jaribu baadaye.\nLimit reached. Try in 60 min.")
        mc=parts[0] if parts else "1"
        save_session({"session_id":sid,"phone":phone,"menu_choice":mc,
            "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
        Log.ok(f"USSD confirm [{phone}] menu={mc}")
        threading.Thread(target=_call_back,args=(phone,sid),daemon=True).start()
        return _tr("END Asante! Tutakupigia sekunde 10.\nThank you! Calling you in 10 seconds.\nSimu ni BURE / Call is FREE.")

    if text.endswith("*0"):
        return _tr("END Umeghairi.\nCancelled. Dial *789*1990# anytime.")

    return _tr("END Chaguo batili.\nInvalid. Dial *789*1990#")

def _tr(b:str):
    r=make_response(b,200); r.headers["Content-Type"]="text/plain"; return r

def _kws(t:str)->list:
    KW=["maji","moto","damu","jeraha","vita","chakula","njaa","hatari",
        "msaada","mgonjwa","fire","water","food","help","sick","danger"]
    return [k for k in KW if k in t.lower()]


# ══════════════════════════════════════════════════════════════
#  _call_back — places outbound call
# ══════════════════════════════════════════════════════════════
def _call_back(phone:str, session_id:str):
    """
    Sleeps 5s then calls phone from YOUR_NUMBER.
    AT posts to your number's Voice callback URL when answered.
    That URL must be set to BASE_URL/voice/answer in AT dashboard.
    """
    Log.info(f"Callback in 5s [{session_id[-8:]}] → {phone}")
    time.sleep(5)

    if not voice_service:
        Log.warn("No voice_service — test audio fallback")
        _fallback(phone, session_id); return

    _pending[phone] = session_id
    Log.info(f"Stored pending {phone} → {session_id[-8:]}")

    try:
        resp    = voice_service.call(callFrom=YOUR_NUMBER, callTo=[phone])
        entries = resp.get("entries",[])
        status  = entries[0].get("status","?") if entries else "no_entries"
        Log.ok(f"Call placed status={status} phone={phone}")
        if status not in ("Queued","Ringing","Success"):
            Log.warn(f"Bad status '{status}' — fallback")
            _fallback(phone, session_id)
    except Exception as e:
        Log.error(f"voice.call: {e}")
        _fallback(phone, session_id)


def _fallback(phone:str, session_id:str):
    wav=_wav()
    if not wav: Log.warn(f"No WAV [{session_id[-8:]}]"); return
    Log.info(f"Audio fallback [{session_id[-8:]}] {os.path.basename(wav)}")
    threading.Thread(target=lambda:(_sleep1(), _translate(session_id,wav,phone)),daemon=True).start()

def _sleep1(): time.sleep(1)

def _translate(sid:str, path:str, phone:str):
    from translator import process_recording
    process_recording(sid, path, phone)


# ══════════════════════════════════════════════════════════════
#  /voice/answer
#
#  THIS IS THE KEY ROUTE.
#  Set your AT virtual number's Voice callback URL to:
#    https://kingo-lik-engine.onrender.com/voice/answer
#
#  AT posts here for BOTH:
#    (A) Someone calls your number (inbound/flash)
#    (B) Your outbound call is answered
#
#  We distinguish them using the "direction" field AT sends:
#    direction = "Inbound"   → someone called us → reject + callback
#    direction = "Outbound"  → we called them, they answered → serve greeting
#
#  If direction is missing (older AT API versions), we fall back to
#  checking _pending dict: if their number is in _pending, it's outbound.
# ══════════════════════════════════════════════════════════════
@app.route("/voice/answer", methods=["POST","GET"])
def voice_answer():
    # Collect all AT fields — AT may send via form or query string
    caller      = (request.values.get("callerNumber")      or "").strip()
    destination = (request.values.get("destinationNumber") or "").strip()
    direction   = (request.values.get("direction")         or "").strip()
    call_state  = (request.values.get("callSessionState")  or "").lower().strip()
    session_id  = (request.args.get("session_id")          or
                   request.values.get("sessionId")         or "").strip()

    # Log everything AT sends so we can debug
    Log.info(
        f"VOICE/ANSWER  caller={caller}  dest={destination}"
        f"  dir={direction}  state={call_state}  sid=[...{(session_id or'?')[-8:]}]"
    )
    Log.info(f"  raw form: {dict(request.form)}")

    # ── Determine if INBOUND or OUTBOUND ─────────────────────
    #
    # AT sends:
    #   Inbound flash:   direction="Inbound",  callerNumber=user_phone, destinationNumber=YOUR_NUMBER
    #   Outbound answer: direction="Outbound", callerNumber=YOUR_NUMBER, destinationNumber=user_phone
    #
    # When direction is missing, check _pending dict.

    is_outbound = False

    if direction.lower() == "outbound":
        is_outbound = True
        Log.info("Direction=Outbound confirmed")
    elif direction.lower() == "inbound":
        is_outbound = False
        Log.info("Direction=Inbound confirmed")
    else:
        # No direction field — check _pending
        user_phone = destination if destination != YOUR_NUMBER else caller
        if user_phone in _pending or caller in _pending:
            is_outbound = True
            Log.info(f"Direction inferred=Outbound (found in _pending)")
        else:
            is_outbound = False
            Log.info("Direction inferred=Inbound (not in _pending)")

    # ── Handle inbound flash ──────────────────────────────────
    if not is_outbound:
        flash_caller = caller  # the person who called your number
        if not flash_caller:
            return reject_xml()
        if ALLOWED and flash_caller not in ALLOWED:
            Log.warn(f"Blocked inbound: {flash_caller}")
            return reject_xml()
        if _limited(flash_caller):
            Log.warn(f"Rate limit inbound: {flash_caller}")
            return reject_xml()

        new_sid = f"flash_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
        from database import save_session
        save_session({"session_id":new_sid,"phone":flash_caller,"menu_choice":"flash",
                      "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
        Log.ok(f"Flash from {flash_caller} — reject + callback in 5s")
        threading.Thread(target=_call_back,args=(flash_caller,new_sid),daemon=True).start()
        return reject_xml()

    # ── Handle outbound answered ──────────────────────────────
    # Resolve session_id
    if not session_id:
        user_phone  = destination if destination != YOUR_NUMBER else caller
        session_id  = (_pending.pop(user_phone, None) or
                       _pending.pop(caller,      None) or
                       _pending.pop(destination,  None) or
                       f"ans_{datetime.datetime.utcnow().strftime('%H%M%S')}")
        Log.info(f"Resolved session_id={session_id[-8:]} from _pending for {user_phone}")

    Log.ok(f"Outbound answered — serving greeting  [{session_id[-8:]}]")
    return greeting_xml(session_id)


# ══════════════════════════════════════════════════════════════
#  /voice/save — AT posts here after user records and presses *
# ══════════════════════════════════════════════════════════════
@app.route("/voice/save", methods=["POST","GET"])
def voice_save():
    from database import update_call_record
    from translator import process_recording

    sid  = (request.args.get("session_id") or request.values.get("sessionId") or "")
    url  = (request.form.get("recordingUrl") or request.values.get("recordingUrl") or "")
    dur  = (request.form.get("durationInSeconds") or request.values.get("durationInSeconds") or "0")

    Log.info(f"VOICE/SAVE  [...{(sid or'?')[-8:]}]  dur={dur}s  url={bool(url)}")

    if url:
        if sid: update_call_record(sid, url, dur)
        s = sid or f"rec_{datetime.datetime.utcnow().strftime('%H%M%S')}"
        Log.ok(f"Translating [{s[-8:]}]  {url[:60]}")
        threading.Thread(target=process_recording,args=(s,url),daemon=True).start()
    else:
        Log.warn(f"No recordingUrl in /voice/save  [{(sid or'?')[-8:]}]")

    return "",200


# ══════════════════════════════════════════════════════════════
#  /sms — Please-Call-Me + inbound SMS trigger
# ══════════════════════════════════════════════════════════════
@app.route("/sms", methods=["POST","GET"])
def sms():
    sender = (request.values.get("from") or "").strip()
    text   = (request.values.get("text") or "").strip()
    Log.info(f"SMS from={sender} text='{text[:60]}'")
    if not sender or (ALLOWED and sender not in ALLOWED) or _limited(sender):
        return "",200
    sid=f"pcm_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
    from database import save_session
    save_session({"session_id":sid,"phone":sender,"menu_choice":"sms",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
    Log.ok(f"SMS trigger from {sender} — calling back in 5s")
    threading.Thread(target=_call_back,args=(sender,sid),daemon=True).start()
    return "",200


# ══════════════════════════════════════════════════════════════
#  WAV picker
# ══════════════════════════════════════════════════════════════
def _wav():
    d=os.path.join(os.getcwd(),"recordings")
    try:
        files=[os.path.join(d,f) for f in os.listdir(d)
               if f.endswith(".wav") and "_clean" not in f and "_raw_clean" not in f
               and not any(f.startswith(p) for p in ("ATUid_","local_test_","url_test_"))]
        return max(files,key=os.path.getmtime) if files else None
    except Exception as e:
        Log.error(f"WAV scan: {e}"); return None


# ══════════════════════════════════════════════════════════════
#  Test routes
# ══════════════════════════════════════════════════════════════
@app.route("/test/local")
def test_local():
    from translator import process_recording
    sid=request.args.get("session_id",f"local_{datetime.datetime.utcnow().strftime('%H%M%S')}")
    p=(os.path.join(os.getcwd(),request.args.get("file")) if request.args.get("file") else _wav())
    if not p or not os.path.exists(p): return jsonify({"error":"No WAV"}),404
    threading.Thread(target=process_recording,args=(sid,p),daemon=True).start()
    return jsonify({"status":"processing","session_id":sid,"file":os.path.basename(p)}),200

@app.route("/test/file/<filename>")
def test_file(filename):
    from translator import process_recording
    sid=f"local_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    p=os.path.join(os.getcwd(),"recordings",filename)
    if not os.path.exists(p):
        return jsonify({"error":f"{filename} not found",
                        "available":[f for f in os.listdir("recordings")
                                     if f.endswith(".wav") and "_clean" not in f]}),404
    threading.Thread(target=process_recording,args=(sid,p),daemon=True).start()
    return jsonify({"status":"processing","file":filename,"session_id":sid}),200

@app.route("/test/call/<path:phone>")
def test_call(phone):
    """
    Full end-to-end test. Calls phone_number via AT.
    Phone rings in ~5s. Answer, speak, press *, see translation on dashboard.
    Requires AT_API_KEY and AT voice callback URL = BASE_URL/voice/answer
    """
    if not voice_service:
        return jsonify({"error":"AT_API_KEY not set on Render — voice disabled"}),500
    sid=f"test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({"session_id":sid,"phone":phone,"menu_choice":"test",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
    threading.Thread(target=_call_back,args=(phone,sid),daemon=True).start()
    return jsonify({"status":"calling","phone":phone,"session":sid,
                    "note":"Answer in ~5s, speak, press *, see translation on /dashboard"}),200

@app.route("/test/url")
def test_url():
    from translator import process_recording
    u=request.args.get("url","")
    if not u: return jsonify({"error":"url required"}),400
    sid=f"url_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    threading.Thread(target=process_recording,args=(sid,u),daemon=True).start()
    return jsonify({"status":"processing","session_id":sid}),200

@app.route("/test/at-config")
def test_at_config():
    """
    Shows exactly what URLs to set in your AT dashboard.
    Visit this after deploying to confirm your setup is correct.
    """
    return jsonify({
        "AT_dashboard_settings": {
            "STEP_1": {
                "where": "AT Dashboard → Voice → Phone Numbers → +254711082547 → Edit",
                "field": "Voice callback URL",
                "value": f"{BASE_URL}/voice/answer",
                "why":   "AT posts here for ALL voice events: inbound flash + outbound answer"
            },
            "STEP_2": {
                "where": "AT Dashboard → USSD → *789*1990# → Edit",
                "field": "Callback URL",
                "value": f"{BASE_URL}/ussd",
                "why":   "AT posts here when user navigates the USSD menu"
            },
            "STEP_3": {
                "where": "AT Dashboard → SMS → Incoming messages (optional)",
                "field": "Callback URL",
                "value": f"{BASE_URL}/sms",
                "why":   "Triggers callback when user sends SMS or Please-Call-Me"
            },
            "STEP_4_test": {
                "url":  f"{BASE_URL}/test/call/+254714137554",
                "what": "Replace with your number. Phone rings in ~5s."
            }
        },
        "env_vars_needed_on_Render": {
            "AT_API_KEY":         "your AT API key",
            "AT_USERNAME":        "kingolik_live (or sandbox)",
            "AT_VIRTUAL_NUMBER":  "+254711082547",
            "BASE_URL":           "https://kingo-lik-engine.onrender.com",
            "GROQ_API_KEY":       "your Groq key for Co-Pilot",
            "GEMINI_API_KEY":     "your Gemini key for translation"
        }
    })


# ── Co-Pilot ──────────────────────────────────────────────────
@app.route("/api/copilot", methods=["POST"])
def copilot_api():
    from copilot import get_copilot_response
    data=request.get_json() or {}
    q=(data.get("query") or data.get("question","")).strip()
    if not q: return jsonify({"error":"query required"}),400
    r=get_copilot_response(q); t=r.get("text","")
    return jsonify({"answer":t,"text":t,"mode":r.get("mode","?"),
                    "reports_analysed":r.get("snapshot",{}).get("total",0),"audio":r.get("audio")})

@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    if not re.match(r'^copilot_\d+\.mp3$',filename): return jsonify({"error":"invalid"}),400
    p=f"/tmp/{filename}"
    return send_file(p,mimetype="audio/mpeg") if os.path.exists(p) else (jsonify({"error":"not found"}),404)


# ── Health ────────────────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
def health():
    if request.method=="POST" and request.form.get("sessionId"): return ussd()
    return jsonify({
        "status":"Kingolik running",
        "ussd":"*789*1990#",
        "flash":YOUR_NUMBER,
        "dashboard":"/dashboard",
        "analytics":"/analytics",
        "config_check":"/test/at-config"
    }),200

@socketio.on("connect")
def on_connect(): Log.ok("WS connected")

@socketio.on("disconnect")
def on_disconnect(): Log.info("WS disconnected")

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    Log.section("Kingolik NGO Voice Bridge")
    Log.ok(f"port={port}  number={YOUR_NUMBER}")
    Log.ok(f"Voice URL → {BASE_URL}/voice/answer")
    Log.ok(f"USSD  URL → {BASE_URL}/ussd")
    Log.divider()
    socketio.run(app,host="0.0.0.0",port=port,debug=False)