from gevent import monkey
monkey.patch_all()

from flask_socketio import SocketIO
from flask import Flask, request, make_response, jsonify, send_file
import africastalking
import os, re, json, datetime, threading, logging, time
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

app      = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

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
BASE_URL           = os.getenv("BASE_URL", "").rstrip("/")
GREETING_AUDIO_URL = os.getenv("GREETING_AUDIO_URL", "")

# Guard against empty BASE_URL
if not BASE_URL:
    BASE_URL = "https://kingo-lik-engine.onrender.com"

# Hard-block the blacklisted number
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

if not ALERT_PHONE:
    print("[WARN] ALERT_PHONE not set or blacklisted — SMS alerts disabled")
else:
    print(f"[OK] ALERT_PHONE={ALERT_PHONE}")

is_ngrok  = "ngrok" in BASE_URL
is_render = "onrender.com" in BASE_URL
is_local  = "127.0.0.1" in BASE_URL or "localhost" in BASE_URL

print(f"[OK] BASE_URL={BASE_URL}")
if is_local:
    print("[CRITICAL] BASE_URL is localhost — AT cannot reach it! Run ngrok and update .env")
elif is_ngrok:
    print("[INFO] Running via ngrok — recording callback will reach this laptop")
elif is_render:
    print("[INFO] Running on Render — all callbacks go to Render")


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
#  Handles: 0714137554 → +254714137554
#           254714137554 → +254714137554
#           +254714137554 → unchanged
# ══════════════════════════════════════════════════════════════
def _norm(phone: str) -> str:
    if not phone: return ""
    p = phone.strip().replace(" ","").replace("-","")
    if p.startswith("+254"): return p
    if p.startswith("254") and len(p) >= 12: return "+" + p
    if p.startswith("0") and len(p) == 10: return "+254" + p[1:]
    return p


# ══════════════════════════════════════════════════════════════
#  Whitelist + rate limiter
# ══════════════════════════════════════════════════════════════
_raw_wl = os.getenv("ALLOWED_PHONES","")
ALLOWED = set(_norm(p.strip()) for p in _raw_wl.split(",") if p.strip()) if _raw_wl else set()
print(f"[SEC] Whitelist: {sorted(ALLOWED)}" if ALLOWED else "[SEC] All phones accepted")

_rs = defaultdict(list); _rl = threading.Lock()
RLIMIT=5; RWIN=3600

def _limited(phone:str)->bool:
    phone=_norm(phone); now=time.time()
    with _rl:
        _rs[phone]=[t for t in _rs[phone] if now-t<RWIN]
        if len(_rs[phone])>=RLIMIT: return True
        _rs[phone].append(now); return False

def _allowed(phone:str)->bool:
    return not ALLOWED or _norm(phone) in ALLOWED


# ══════════════════════════════════════════════════════════════
#  Urgent keywords
# ══════════════════════════════════════════════════════════════
URGENT_KW = [
    "help","msaada","haraka","emergency","hatari","moto","fire",
    "damu","blood","jeraha","injury","attack","shambulio","wezi",
    "thieves","robbery","vita","violence","mgonjwa","sick","hospital",
    "njaa","hunger","maji","water","missing","kupotea","police","polisi",
    "ninavamiwa","navamiwa","attacked","danger","sos","mjamzito","pregnant"
]

def _kws(text:str)->list:
    t=text.lower()
    return list(set([k for k in URGENT_KW if k in t]))


# ══════════════════════════════════════════════════════════════
#  State
# ══════════════════════════════════════════════════════════════
_answered: set = set()   # AT sessionIds already served greeting XML
_pending:  dict = {}     # phone → our_session_id


# ══════════════════════════════════════════════════════════════
#  XML helpers
# ══════════════════════════════════════════════════════════════
def _xml(body:str):
    r=make_response(body,200); r.headers["Content-Type"]="application/xml"; return r

def _reject():
    return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>')

def _greeting(session_id:str):
    """
    Greeting XML served when outbound call is answered.
    finishOnKey="#*" — user presses # or * to end recording.
    Hanging up ALSO triggers recording callback on most networks.
    callbackUrl points to /voice/save which handles the recording.
    """
    cb = f"{BASE_URL}/voice/save?session_id={session_id}"
    if GREETING_AUDIO_URL:
        voice_xml = f'<Play>{GREETING_AUDIO_URL}</Play>'
    else:
        voice_xml = (
            '<Say voice="woman" playBeep="false">'
            'Habari, karibu King-olik. '
            'Simu hii ni bure kwako. '
            'Tafadhali sema ujumbe wako baada ya mlio. '
            'Bonyeza hash ukimaliza. Asante.'
            '</Say>'
        )
    return _xml(f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {voice_xml}
  <Record
    finishOnKey="#*"
    maxLength="120"
    trimSilence="true"
    playBeep="true"
    callbackUrl="{cb}"
  />
</Response>""")

def _empty():
    return make_response("",200)

def _tr(b:str):
    r=make_response(b,200); r.headers["Content-Type"]="text/plain"; return r


# ══════════════════════════════════════════════════════════════
#  Werkzeug filter
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
    phone = _norm(request.form.get("phoneNumber",""))
    text  = request.form.get("text","").strip()
    Log.ussd(sid,phone,text)

    if not _allowed(phone):
        return _tr("END System restricted to authorised personnel only.")

    parts=text.split("*") if text else []

    if text=="":
        return _tr("CON Karibu King'olik.\n1. Afya/Health\n2. Chakula/Food\n3. Usalama/Security\n4. Ingineo/Other")

    if text in("1","2","3"):
        lbl={"1":"Afya/Health","2":"Chakula/Food","3":"Usalama/Security"}
        return _tr(f"CON {lbl[text]}\nTutakupigia ndani ya sekunde 10.\n1. Thibitisha/Confirm\n0. Ghairi/Cancel")

    if text=="4":
        return _tr("CON Andika shida kwa ufupi:\nType your issue briefly:")

    if text.startswith("4*") and len(parts)>=2:
        raw="*".join(parts[1:])
        from database import save_session as sv
        sv({"session_id":sid,"phone":phone,"menu_choice":"4",
            "timestamp":datetime.datetime.utcnow().isoformat(),"status":"text_report"})
        Log.ok(f"Text report [{phone}] '{raw[:40]}' — translating...")
        threading.Thread(target=_translate_text,args=(sid,raw,phone),daemon=True).start()
        return _tr("END Ahsante. Tumepokea ripoti yako.\nThank you. Report received.")

    if text.endswith("*1"):
        if _limited(phone):
            return _tr("END Limit imefikiwa. Jaribu baadaye.\nLimit reached. Try in 60 min.")
        mc=parts[0] if parts else "1"
        save_session({"session_id":sid,"phone":phone,"menu_choice":mc,
            "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
        Log.ok(f"USSD confirm [{phone}] menu={mc}")
        socketio.start_background_task(_call_back,phone,sid)
        return _tr("END Asante! Tutakupigia sekunde 10.\nThank you! Calling in 10 seconds. BURE/FREE.")

    if text.endswith("*0"):
        return _tr("END Umeghairi.\nCancelled. Dial *789*1990# anytime.")

    return _tr("END Chaguo batili.\nInvalid. Dial *789*1990#")


def _translate_text(session_id:str, raw:str, phone:str):
    """Translate USSD option-4 text report via Gemini. Retries 3x on 503."""
    kws=_kws(raw); translation=raw; lang="sw"; conf="medium"; engine="ussd_text"
    key=os.environ.get("GEMINI_API_KEY","")
    if key:
        for attempt in range(3):
            try:
                from google import genai
                from google.genai import types
                client=genai.Client(api_key=key)
                prompt=(
                    "Translate this humanitarian field report to English. "
                    "May be Swahili, Turkana, Somali, Arabic, or mixed. "
                    'Return ONLY a JSON object: {"detected_language":"sw","translation":"...","urgent_keywords":[],"confidence":"high"}\n\n'
                    f"Text: {raw}"
                )
                resp=client.models.generate_content(
                    model="models/gemini-2.5-flash",
                    contents=[types.Content(parts=[types.Part(text=prompt)])]
                )
                raw_resp=re.sub(r"```json|```","",resp.text or "").strip()
                m=re.search(r'\{.*\}',raw_resp,re.DOTALL)
                if not m:
                    raise ValueError(f"No JSON in response: {raw_resp[:80]}")
                p=json.loads(m.group())
                translation=p.get("translation",raw)
                lang=p.get("detected_language","sw")
                kws=list(set(kws+(p.get("urgent_keywords") or [])))
                conf=p.get("confidence","medium")
                engine="gemini_text"
                Log.ok(f"Gemini translated [{session_id[-8:]}]: '{translation[:60]}'")
                break
            except Exception as e:
                if "503" in str(e) and attempt<2:
                    Log.warn(f"Gemini 503 attempt {attempt+1}/3 — retry 3s"); time.sleep(3)
                else:
                    Log.warn(f"Gemini failed: {e}"); break

    result={"transcript":raw,"detected_language":lang,"translation":translation,
            "urgent_keywords":kws,"confidence":conf,"engine":engine,
            "requires_review":conf=="low","is_text_report":True}
    from database import save_translation
    save_translation(session_id,result)
    Log.ok(f"Text report saved [{session_id[-8:]}]  lang={lang}")
    if kws and ALERT_PHONE:
        threading.Thread(target=_alert_sms,args=(session_id,phone,result),daemon=True).start()


def _alert_sms(session_id:str, caller:str, result:dict):
    if not sms_service or not ALERT_PHONE: return
    try:
        kws=result.get("urgent_keywords",[]); t=result.get("translation","")[:100]
        msg=(f"KINGOLIK URGENT\nFrom:{caller}\nAlert:{','.join(kws[:5])}\nSaid:{t}\nRef:{session_id[-8:]}")
        resp=sms_service.send(message=msg,recipients=[ALERT_PHONE])
        recips=resp.get("SMSMessageData",{}).get("Recipients",[])
        status=recips[0].get("status","?") if recips else "no_recipients"
        if status=="Success":
            Log.ok(f"Alert SMS sent → {ALERT_PHONE}")
        else:
            Log.warn(f"Alert SMS status={status} number={ALERT_PHONE}")
    except Exception as e:
        Log.error(f"Alert SMS: {e}")


# ══════════════════════════════════════════════════════════════
#  Outbound call with retry
# ══════════════════════════════════════════════════════════════
def _call_back(phone:str, session_id:str, max_attempts:int=3):
    phone=_norm(phone)
    for attempt in range(1,max_attempts+1):
        wait=3 if attempt==1 else 30
        Log.info(f"Callback wait={wait}s [{session_id[-8:]}] → {phone}  attempt={attempt}/{max_attempts}")
        time.sleep(wait)

        if not voice_service:
            Log.warn("No voice_service — test audio fallback")
            _fallback(phone,session_id); return

        _pending[phone]=session_id
        Log.info(f"Stored _pending {phone} → {session_id[-8:]}")

        try:
            resp=voice_service.call(callFrom=YOUR_NUMBER,callTo=[phone])
            entries=resp.get("entries",[])
            status=entries[0].get("status","?") if entries else "no_entries"
            Log.ok(f"Call placed status={status} phone={phone} attempt={attempt}")
            if status in ("Queued","Ringing","Success"):
                return
            Log.warn(f"Bad status '{status}' attempt {attempt}")
            if attempt==max_attempts: _fallback(phone,session_id)
        except Exception as e:
            Log.error(f"voice.call attempt {attempt}: {e}")
            if attempt==max_attempts: _fallback(phone,session_id)


def _fallback(phone:str, session_id:str):
    wav=_wav()
    if not wav: Log.warn(f"No WAV for fallback [{session_id[-8:]}]"); return
    Log.info(f"Audio fallback [{session_id[-8:]}] {os.path.basename(wav)}")
    def run():
        time.sleep(1)
        from translator import process_recording
        process_recording(session_id,wav,phone)
    threading.Thread(target=run,daemon=True).start()


# ══════════════════════════════════════════════════════════════
#  /voice/answer
#
#  Set in AT dashboard: Voice → +254711082547 → Voice callback URL:
#  https://kingo-lik-engine.onrender.com/voice/answer
#
#  isActive=1 + inbound  → flash call → reject + callback
#  isActive=1 + outbound → answered  → serve greeting XML
#  isActive=0            → ended     → check for recordingUrl
#
#  NOTE: AT sandbox sometimes posts recordingUrl here too.
#  We check for it in every request.
# ══════════════════════════════════════════════════════════════
@app.route("/voice/answer", methods=["POST","GET"])
def voice_answer():
    caller    = _norm(request.values.get("callerNumber","") or "")
    dest      = _norm(request.values.get("destinationNumber","") or "")
    direction = (request.values.get("direction","") or "").lower()
    state     = (request.values.get("callSessionState","") or "")
    is_active = (request.values.get("isActive","0") or "0")
    at_sid    = (request.values.get("sessionId","") or "")

    rec_url = (
        request.form.get("recordingUrl") or request.form.get("RecordingUrl") or
        request.values.get("recordingUrl") or request.values.get("RecordingUrl") or ""
    )
    dur = (request.form.get("durationInSeconds") or
           request.values.get("durationInSeconds") or "0")

    Log.info(
        f"VOICE  caller={caller}  dest={dest}  dir={direction}"
        f"  state={state}  isActive={is_active}  rec={'YES' if rec_url else 'no'}"
        f"  atSid=[...{at_sid[-8:] if at_sid else '?'}]"
    )

    # Recording arrived here (AT sandbox sometimes posts it here)
    if rec_url:
        Log.ok(f"Recording in /voice/answer  dur={dur}s")
        user_phone = dest if dest != YOUR_NUMBER else caller
        sid = (request.args.get("session_id") or
               _pending.pop(user_phone,None) or
               _pending.pop(caller,None) or
               f"rec_{datetime.datetime.utcnow().strftime('%H%M%S')}")
        _handle_recording(sid, rec_url, dur)
        return _empty()

    if is_active != "1":
        Log.info(f"  isActive=0 state={state} — empty")
        return _empty()

    # Inbound flash → reject + callback
    if direction == "inbound":
        if not caller: return _reject()
        if not _allowed(caller): Log.warn(f"Blocked: {caller}"); return _reject()
        if _limited(caller): Log.warn(f"Rate limit: {caller}"); return _reject()
        new_sid=f"flash_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
        from database import save_session
        save_session({"session_id":new_sid,"phone":caller,"menu_choice":"flash",
                      "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
        Log.ok(f"Flash from {caller} — callback in 3s")
        socketio.start_background_task(_call_back,caller,new_sid)
        return _reject()

    # Outbound answered → serve greeting ONCE
    if at_sid in _answered:
        Log.info(f"  Duplicate answer [{at_sid[-8:]}] — empty")
        return _empty()
    _answered.add(at_sid)

    user_phone = dest if dest != YOUR_NUMBER else caller
    our_sid = (
        _pending.pop(user_phone,None) or
        _pending.pop(caller,None) or
        _pending.pop(dest,None) or
        f"ans_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    )
    Log.ok(f"OUTBOUND ANSWERED  user={user_phone}  session=[...{our_sid[-8:]}]")
    return _greeting(our_sid)


# ══════════════════════════════════════════════════════════════
#  /voice/save — AT posts recording here after user presses #/*
# ══════════════════════════════════════════════════════════════
@app.route("/voice/save", methods=["POST","GET"])
def voice_save():
    Log.ok("VOICE/SAVE HIT ← recording arrived")
    Log.info(f"  form={dict(request.form)}")

    sid = (request.args.get("session_id") or request.values.get("sessionId") or "")
    url = (
        request.form.get("recordingUrl") or request.form.get("RecordingUrl") or
        request.values.get("recordingUrl") or request.values.get("RecordingUrl") or
        request.args.get("recordingUrl") or ""
    )
    dur = (request.form.get("durationInSeconds") or
           request.values.get("durationInSeconds") or "0")

    Log.info(f"  sid={sid or 'NONE'}  url={'YES '+dur+'s' if url else 'NONE'}")

    if url:
        _handle_recording(sid, url, dur)
    else:
        Log.warn("No recordingUrl — user may not have pressed # after speaking")
        Log.warn(f"Tell user: speak then press # to save. Recording goes to {BASE_URL}/voice/save")

    return _empty()


def _handle_recording(session_id:str, recording_url:str, duration:str="0"):
    """
    Downloads AT recording, saves as {session_id}_raw.wav,
    runs translation pipeline, pushes result to dashboard.
    """
    from database import update_call_record
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
                    Log.warn(f"Download HTTP {r.status_code} — will try URL directly")
            except Exception as e:
                Log.error(f"Download failed: {e}")
        elif os.path.exists(recording_url):
            # Local file path (test mode)
            local_path = recording_url
            downloaded = True

        source = local_path if downloaded else recording_url
        try:
            from translator import process_recording
            process_recording(sid, source)
            Log.ok(f"Translation pipeline started [{sid[-8:]}]")
        except Exception as e:
            Log.error(f"Translation failed [{sid[-8:]}]: {e}")

    threading.Thread(target=run, daemon=True).start()


# ══════════════════════════════════════════════════════════════
#  /sms — Please-Call-Me trigger + urgent keyword detection
# ══════════════════════════════════════════════════════════════
@app.route("/sms", methods=["POST","GET"])
def sms():
    sender=_norm(request.values.get("from") or request.values.get("fromNumber") or "")
    text=(request.values.get("text") or "").strip()
    Log.info(f"SMS from={sender}  text='{text[:80]}'")

    if not sender: return _empty()
    if not _allowed(sender): Log.warn(f"SMS blocked: {sender}"); return _empty()
    if _limited(sender): Log.warn(f"SMS rate limit: {sender}"); return _empty()

    kws=_kws(text)
    sid=f"sms_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
    from database import save_session
    save_session({"session_id":sid,"phone":sender,"menu_choice":"sms",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})

    if kws and text:
        Log.ok(f"SMS urgent [{kws}] from {sender} — saving report + callback")
        threading.Thread(target=_translate_text,args=(sid,text,sender),daemon=True).start()
    else:
        Log.ok(f"SMS/PCM from {sender} — callback in 3s")

    socketio.start_background_task(_call_back,sender,sid)
    return _empty()


# ══════════════════════════════════════════════════════════════
#  WAV picker (test fallback only)
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
                        "available":[f for f in os.listdir("recordings") if f.endswith(".wav") and "_clean" not in f]}),404
    threading.Thread(target=process_recording,args=(sid,p),daemon=True).start()
    return jsonify({"status":"processing","file":filename,"session_id":sid}),200

@app.route("/test/call/<path:phone>")
def test_call(phone):
    """
    Full E2E test.
    Flow: Answer phone → hear greeting → speak → press # → see translation on /dashboard
    """
    phone=_norm(phone)
    if not voice_service:
        return jsonify({"error":"AT_API_KEY not set"}),500
    sid=f"test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({"session_id":sid,"phone":phone,"menu_choice":"test",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
    threading.Thread(target=_call_back,args=(phone,sid),daemon=True).start()
    return jsonify({
        "status":"calling","phone":phone,"session":sid,
        "step1":"Answer phone in ~3 seconds",
        "step2":"Hear greeting + beep",
        "step3":"Speak your message",
        "step4":"Press # to end recording (IMPORTANT)",
        "step5":"Watch terminal for: VOICE/SAVE HIT",
        "step6":"Translation appears on /dashboard within 30s"
    }),200

@app.route("/test/sim-save")
def test_sim_save():
    """
    Simulate AT posting a recording — tests translation pipeline without a real call.
    GET /test/sim-save to use most recent session + best available test audio.
    """
    sid=request.args.get("session_id","")
    if not sid:
        from database import get_all_sessions
        sessions=get_all_sessions()
        recent=[s for s in sessions if s.get("status") in ("pending_call","recorded")]
        sid=recent[0]["session_id"] if recent else f"simtest_{datetime.datetime.utcnow().strftime('%H%M%S')}"

    test_wav=_wav()
    if test_wav:
        Log.ok(f"Sim-save: {os.path.basename(test_wav)}")
        _handle_recording(sid, test_wav, "15")
        source=os.path.basename(test_wav)
    else:
        Log.ok("Sim-save: no local WAV — cannot test without audio file")
        return jsonify({"error":"No WAV files in recordings/ folder",
                        "fix":"Run /test/call/+YOUR_NUMBER first to create a recording"}),404

    return jsonify({
        "status":     "Pipeline test started",
        "session_id": sid,
        "source":     source,
        "watch":      "/dashboard — translation card should appear within 30 seconds",
        "terminal":   "Look for [TRANSLATE] and [DB] Translation saved in logs"
    }),200

@app.route("/test/sms/<phone>")
def test_sms(phone):
    """Simulate PCM from a number — triggers callback."""
    phone=_norm(phone)
    sid=f"sms_test_{datetime.datetime.utcnow().strftime('%H%M%S')}"
    from database import save_session
    save_session({"session_id":sid,"phone":phone,"menu_choice":"sms_test",
                  "timestamp":datetime.datetime.utcnow().isoformat(),"status":"pending_call"})
    socketio.start_background_task(_call_back,phone,sid)
    return jsonify({"status":"callback triggered","phone":phone,"session":sid}),200

@app.route("/test/at-config")
def at_config():
    return jsonify({
        "AT_dashboard_settings": {
            "voice_callback_url": f"{BASE_URL}/voice/answer",
            "ussd_callback_url":  f"{BASE_URL}/ussd",
            "sms_callback_url":   f"{BASE_URL}/sms",
        },
        "recording_flow": {
            "step1": "User answers callback → hears greeting + beep",
            "step2": "User speaks their message",
            "step3": "User presses # (hash key) to end recording",
            "step4": f"AT posts recording to: {BASE_URL}/voice/save",
            "step5": "Server downloads audio file from AT",
            "step6": "Gemini or Whisper translates audio to English",
            "step7": "Translation pushed to /dashboard via WebSocket"
        },
        "test_endpoints": {
            "call":     f"{BASE_URL}/test/call/+YOUR_PHONE",
            "sim_save": f"{BASE_URL}/test/sim-save",
            "local":    f"{BASE_URL}/test/local"
        },
        "env_status": {
            "ALERT_PHONE":     ALERT_PHONE or "NOT SET",
            "BASE_URL":        BASE_URL,
            "ngrok":           is_ngrok,
            "render":          is_render,
            "whitelist_count": len(ALLOWED),
            "whitelist":       sorted(ALLOWED) if ALLOWED else "all phones accepted"
        },
        "render_env_vars_needed": {
            "ALLOWED_PHONES":  "comma separated, no spaces, +254 prefix",
            "ALERT_PHONE":     "+254714137554 (your number)",
            "AT_API_KEY":      "your AT production key",
            "BASE_URL":        "https://kingo-lik-engine.onrender.com",
            "GEMINI_API_KEY":  "your Gemini key",
            "GROQ_API_KEY":    "your Groq key"
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
                    "reports_analysed":r.get("snapshot",{}).get("total",0),
                    "audio":r.get("audio")})

@app.route("/api/copilot/audio/<filename>")
def copilot_audio(filename):
    if not re.match(r'^copilot_\d+\.mp3$',filename): return jsonify({"error":"invalid"}),400
    p=f"/tmp/{filename}"
    return send_file(p,mimetype="audio/mpeg") if os.path.exists(p) else (jsonify({"error":"not found"}),404)


# ── Health ────────────────────────────────────────────────────
@app.route("/", methods=["GET","POST"])
def health():
    if request.method=="POST" and request.form.get("sessionId"): return ussd()
    return jsonify({"status":"Kingolik running","ussd":"*789*1990#","flash":YOUR_NUMBER,
                    "dashboard":"/dashboard","analytics":"/analytics",
                    "config":"/test/at-config"}),200

@socketio.on("connect")
def on_connect(): Log.ok("WS connected")

@socketio.on("disconnect")
def on_disconnect(): Log.info("WS disconnected")

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    Log.section("Kingolik NGO Voice Bridge")
    Log.ok(f"port={port}  number={YOUR_NUMBER}")
    Log.ok(f"alert={ALERT_PHONE or 'disabled'}  greeting={'custom' if GREETING_AUDIO_URL else 'AT TTS'}")
    Log.ok(f"base={BASE_URL}")
    Log.divider()
    
    # This will now safely run using Gevent!
    socketio.run(app, host="0.0.0.0", port=port, debug=False)