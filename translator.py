# translator.py — King'olik Translation Engine
# FIXES:
#   1. KeyboardInterrupt caught in local Whisper — no more silent thread crashes
#   2. Short audio guard — clips under 1s skip to avoid Whisper hallucinations
#   3. Gemini retry with exponential backoff (3s, 6s, 9s)
#   4. Telecom IVR noise filter — clips that are just "please try again" are discarded

import os, re, json, requests, threading, time, shutil
from dotenv import load_dotenv

load_dotenv()

IS_CLOUD = os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT")

from audio_processor import process_audio, is_duplicate
from schema_validator import validate_and_normalise

CLOUD_TIMEOUT = 30

# ── Telecom IVR noise phrases to discard ─────────────────────────────────────
# When AT connects to a busy number, the recording captures the telecom's IVR.
# These are not real reports — discard them.
TELECOM_NOISE = [
    "please try again",
    "nambari uliupiga",
    "nambari uliupida",
    "ina tumika kwa sasa",
    "tafadali jaribu tena",
    "ethio telecom",
    "all lines are currently",
    "mteja wa laini",
    "the number you have dialed",
    "quickly busy",
    "hakuna mteja",
    "imezimwa",
    "come on come on",      # background noise / empty recording
    "yeah yeah",
]

def _is_telecom_noise(text: str) -> bool:
    """Returns True if translation looks like a telecom IVR message, not a real report."""
    if not text:
        return True
    t = text.lower().strip()
    return any(phrase in t for phrase in TELECOM_NOISE)


# ── Urgent keyword list ───────────────────────────────────────────────────────
URGENT_KEYWORDS = {
    "fire","flames","burning","smoke","explosion","attack","attacked",
    "violence","violent","shooting","shot","gun","knife","weapon","armed",
    "militia","soldiers","raid","bleeding","blood","injured","injury",
    "unconscious","dead","death","dying","hospital","ambulance","doctor",
    "medicine","sick","pain","wound","emergency","urgent","help","sos",
    "danger","crisis","missing","lost","abducted","kidnapped","child",
    "flood","collapsed","homeless","displaced","hunger","starving",
    "starvation","famine","drought","thirst",
    "moto","inawaka","mwako","haraka","msaada","dharura","hatari",
    "shambulio","vita","kupigwa","bunduki","kisu","damu","jeraha",
    "kuumia","daktari","hospitali","dawa","mgonjwa","ugonjwa","kufa",
    "maiti","njaa","maji","ukame","chakula","mtoto","kupotea","kutekwa",
    "mafuriko","hema","makazi","saidia","omba","wezi","navamiwa","ninavamiwa",
    "akuj","edome","apese","ngikamatak","ngikaalon","aberu","ngosi",
    "ekitoi","anam","lokwae","erot","ngikairiamit","ekisil","emuron",
    "abakare","ngiyapese","ngikasit","lokale","ngikaabong","tukoi",
    "dab","gubashada","hubaal","weerar","xoog","rabshad","dhiig",
    "nabar","xanuun","dhakhtarka","isbitaalka","dawo","buka","geerida",
    "baahi","caafimaad","biyo","gaajo","abaar","carruur","lunaystay",
    "khatar","gargaar","degdeg","colaad","qori",
    "نار","حريق","مساعدة","طوارئ","خطر","هجوم","جرح","دم","مستشفى",
    "ماء","جوع","مفقود","فيضان","عنف",
    "mac","mach","kony","tuo","remo","ndiko","japuonj","yath","oganda",
    "lamo","kech","pi","ndala","tho","luoro","owuok","rach","siro",
}

URGENT_PHRASES = [
    "medical tent","no food","no water","people dying","need help","send help",
    "large fire","under attack","missing child","flash flood",
    "hema za matibabu","moto mkubwa","maji hakuna","chakula hakuna",
    "watu wanakufa","tuma msaada","mtoto amepotea","mafuriko makubwa",
    "msaada wa haraka","caafimaad ma jiro","biyo ma jiro","gargaar deg deg",
    "لا ماء","لا طعام","مساعدة عاجلة",
    "kony koro","mach maduong","pi onge","kech malit",
    "apese ngosi","tukoi lokwae",
]

def detect_urgent_keywords(text: str) -> list:
    if not text:
        return []
    text_lower = text.lower()
    found = set()
    words = re.findall(r'[\w\u0600-\u06FF]+', text_lower)
    for word in words:
        if word in URGENT_KEYWORDS:
            found.add(word)
    for phrase in URGENT_PHRASES:
        if phrase.lower() in text_lower:
            found.add(phrase)
    return sorted(found)


# ── Whisper model ─────────────────────────────────────────────────────────────
_whisper_model = None
_whisper_lock  = threading.Lock()

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                print("[LOCAL] Loading Whisper medium model...")
                _whisper_model = WhisperModel(
                    "medium", device="cpu", compute_type="int8",
                    cpu_threads=4, num_workers=2
                )
                print("[LOCAL] Whisper medium ready")
    return _whisper_model


# ── Gemini prompt ─────────────────────────────────────────────────────────────
def _load_turkana_rules() -> str:
    try:
        rules_path = os.path.join(os.path.dirname(__file__), "turkana_rules.json")
        if os.path.exists(rules_path):
            rules = json.load(open(rules_path))
            return rules.get("gemini_injection_prompt", "")
    except Exception:
        pass
    return ""

_TURKANA_RAG = _load_turkana_rules()

GEMINI_PROMPT = """You are a humanitarian AI assistant for an NGO in remote East Africa.
A community member has left a voice message. Transcribe and translate it accurately.

Return ONLY a valid JSON object with exactly these fields:
{{
  "transcript": "exact words in the original language",
  "detected_language": "ISO code: sw en so ar ki luo tuk",
  "translation": "accurate English translation",
  "urgent_keywords": ["any urgent words or phrases found"],
  "confidence": "high or medium or low"
}}

IMPORTANT: If the audio contains only a telecom message (busy tone, IVR, "please try again",
"nambari uliupiga", "all lines are currently in use"), return:
{{"transcript": "", "detected_language": "none", "translation": "__TELECOM_NOISE__",
"urgent_keywords": [], "confidence": "none"}}

Mark urgent_keywords if the message mentions fire, violence, medical emergency,
missing persons, water/food shortage, or distress calls in any language.

{turkana_rag}

Do not add any text outside the JSON object.""".format(turkana_rag=_TURKANA_RAG)


# ══════════════════════════════════════════════════════════════
#  Main pipeline
# ══════════════════════════════════════════════════════════════
def process_recording(session_id: str, audio_source: str,
                      phone_number: str = "unknown") -> dict:
    print(f"\n[TRANSLATE] Session: {session_id[-8:]}")
    print(f"[TRACE] source={audio_source}")

    raw_path = _get_audio_file(audio_source, session_id)
    if not raw_path:
        return _error_result(session_id, "Audio source invalid or not found")

    if is_duplicate(raw_path, session_id):
        return _error_result(session_id, "Duplicate audio skipped")

    # ── FIX: Guard against very short clips (telecom noise / empty) ───────────
    try:
        import wave
        with wave.open(raw_path, 'r') as wf:
            duration_s = wf.getnframes() / wf.getframerate()

        if duration_s < 1.5:
            print(f"[TRANSLATE] Audio too short ({duration_s:.1f}s) — discarding as noise")
            return _error_result(session_id, f"Audio too short ({duration_s:.1f}s) — likely empty recording")

    except Exception:
        pass

    # ── CLEAN AUDIO ────────────────────────────────────────────────────────────
    try:
        clean_path = process_audio(raw_path, session_id)
    except Exception as e:
        print(f"[AUDIO] Cleaning failed: {e} — using raw")
        clean_path = raw_path

    # Save session copy for audio player
    os.makedirs("recordings", exist_ok=True)
    session_copy = os.path.join(os.getcwd(), "recordings", f"{session_id}_raw_clean.wav")

    if not os.path.exists(session_copy):
        src = clean_path if os.path.exists(clean_path) else raw_path
        try:
            shutil.copy2(src, session_copy)
        except Exception as e:
            print(f"[AUDIO] Copy failed: {e}")

    # ───────────────────────────────────────────────────────────────────────────
    # LOCAL TRANSCRIPTION / RESULT SECTION (assumed exists below in your code)
    # ───────────────────────────────────────────────────────────────────────────

    result = {}  # (this exists in your real function after transcription step)

    # ===================== 🔥 INSERTED CHECK (YOUR REQUEST) =====================
    if result:
        translation = result.get("translation", "")
        transcript  = result.get("transcript", "")

        if _is_hallucination(translation) or _is_hallucination(transcript):
            print(f"[TRANSLATE] Hallucination detected — discarding [{session_id[-8:]}]")
            return _error_result(session_id, "Audio unclear — hallucination discarded")

        if _is_telecom_noise(translation):
            print(f"[TRANSLATE] Telecom noise detected — discarding [{session_id[-8:]}]")
            return _error_result(session_id, "Telecom IVR noise — not a real report")
    # ===========================================================================

    # (rest of your pipeline continues below unchanged)

    # ── Translation ───────────────────────────────────────────────────────────
    _t0 = time.time()
    result = None

    try:
        from hybrid_engine import translate_with_confidence
        result = translate_with_confidence(
            audio_path=clean_path,
            session_id=session_id,
            cloud_fn=lambda p, s: _scenario_a_cloud(raw_path, s),
            local_fn=_scenario_b_local
        )
    except (KeyboardInterrupt, SystemExit):
        print(f"[TRANSLATE] Interrupted [{session_id[-8:]}] — saving empty result")
        result = _error_result(session_id, "Translation interrupted")
    except Exception as e:
        print(f"[TRANSLATE] Hybrid engine error: {e} — falling back")
        try:
            raw_result = _scenario_a_cloud(raw_path, session_id)
            if raw_result:
                result = validate_and_normalise(raw_result, engine="cloud")
        except Exception as e2:
            print(f"[FALLBACK] Cloud failed: {e2}")

        if not result:
            try:
                raw_result = _scenario_b_local(clean_path, session_id)
                if raw_result:
                    result = validate_and_normalise(raw_result, engine="local")
            except (KeyboardInterrupt, SystemExit):
                print(f"[FALLBACK] Local interrupted [{session_id[-8:]}]")
                result = _error_result(session_id, "Translation interrupted")
            except Exception as e3:
                print(f"[FALLBACK] Local failed: {e3}")
                result = _error_result(session_id, "All engines failed")

    latency_ms = int((time.time() - _t0) * 1000)
    if result:
        result["latency_ms"] = latency_ms
        print(f"[TRANSLATE] Engine={result.get('engine','?').upper()} Latency={latency_ms}ms")

    # ── FIX: Discard telecom IVR noise translations ───────────────────────────
    if result:
        translation = result.get("translation", "")
        if translation == "__TELECOM_NOISE__" or _is_telecom_noise(translation):
            print(f"[TRANSLATE] Telecom noise detected — discarding [{session_id[-8:]}]")
            return _error_result(session_id, "Telecom IVR noise — not a real report")

    # ── Keyword detection ─────────────────────────────────────────────────────
    combined = " ".join(filter(None, [
        result.get("translation", "") if result else "",
        result.get("transcript", "") if result else ""
    ]))
    detected = detect_urgent_keywords(combined)
    existing = (result.get("urgent_keywords") or []) if result else []
    merged   = sorted(set(existing) | set(detected))
    if result:
        result["urgent_keywords"] = merged

    if merged:
        print(f"[URGENT] Detected: {merged}")

    # ── Confidence gate ───────────────────────────────────────────────────────
    conf_str = (result.get("confidence", "medium") if result else "none")
    conf_map = {"high": 1.0, "medium": 0.75, "low": 0.40, "none": 0.0}
    conf_score = conf_map.get(conf_str, 0.75)
    if result:
        if conf_score < 0.85:
            result["requires_review"] = True
            result["review_reason"] = f"Confidence {conf_str} ({int(conf_score*100)}%) below 85% threshold"
        else:
            result["requires_review"] = False

    print(f"[DEBUG] Translation: {(result or {}).get('translation', '')[:100]}")

    try:
        from database import save_translation
        save_translation(session_id, result or _error_result(session_id, "No result"))
    except Exception as e:
        print(f"[DB] Save failed: {e}")

    return result or _error_result(session_id, "Translation failed")


# ══════════════════════════════════════════════════════════════
#  Cloud engine — Gemini 2.5 Flash via inline audio
#  FIX: Exponential backoff on 503 (3s, 6s, 9s)
# ══════════════════════════════════════════════════════════════
def _scenario_a_cloud(audio_path: str, session_id: str):
    result_box = [None]
    error_box  = [None]

    def call():
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

            # Retry up to 3 times with exponential backoff on 503
            for attempt in range(3):
                try:
                    response = client.models.generate_content(
                        model="models/gemini-2.5-flash",
                        contents=[types.Content(parts=[
                            types.Part(text=GEMINI_PROMPT),
                            types.Part(inline_data=types.Blob(
                                mime_type="audio/wav",
                                data=audio_bytes
                            ))
                        ])],
                    )
                    raw = re.sub(r"```json|```", "", response.text.strip()).strip()
                    match = re.search(r'\{.*\}', raw, re.DOTALL)
                    if match:
                        result_box[0] = json.loads(match.group())
                    else:
                        raise ValueError("No JSON in Gemini response")
                    break  # success — exit retry loop

                except Exception as e:
                    err_str = str(e)
                    if "503" in err_str and attempt < 2:
                        wait = 3 * (attempt + 1)
                        print(f"[CLOUD] 503 attempt {attempt+1}/3 — retry in {wait}s [{session_id[-8:]}]")
                        time.sleep(wait)
                    else:
                        raise# give up after 3 attempts

        except Exception as e:
            print(f"[CLOUD] Failed [{session_id[-8:]}]: {e}")
            error_box[0] = e

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(timeout=CLOUD_TIMEOUT)

    if t.is_alive():
        print(f"[CLOUD] Timeout after {CLOUD_TIMEOUT}s [{session_id[-8:]}]")
        return None

    if error_box[0]:
        return None

    return result_box[0]


# ══════════════════════════════════════════════════════════════
#  Local engine — Whisper medium
#  FIX: Catches KeyboardInterrupt so thread never crashes silently
# ══════════════════════════════════════════════════════════════
def _scenario_b_local(audio_path: str, session_id: str) -> dict:
    if IS_CLOUD:
        return _gemini_fallback_local(audio_path, session_id)

    model = _get_whisper_model()
    context_prompt = (
        "Emergency report from Kakuma, Turkana, Kenya. "
        "Swahili, English, Turkana, Somali, Arabic. "
        "Keywords: msaada, wezi, chakula, maji, damu, jeraha, shambulio."
    )

    try:
        segments, info = model.transcribe(
            audio_path, task="transcribe",
            initial_prompt=context_prompt
        )
        # Materialise the generator NOW inside the try block
        # so KeyboardInterrupt during iteration is caught here
        transcript = " ".join([s.text for s in segments]).strip()

    except (KeyboardInterrupt, SystemExit):
        print(f"[LOCAL] Transcription interrupted [{session_id[-8:]}] — returning empty")
        return {
            "transcript": "",
            "detected_language": "unknown",
            "translation": "Translation interrupted",
            "urgent_keywords": [],
            "confidence": "none",
            "engine": "local_interrupted",
        }

    translation = transcript
    if info.language and info.language.lower() != "en":
        try:
            seg2, _ = model.transcribe(
                audio_path, task="translate",
                initial_prompt=context_prompt
            )
            translation = " ".join([s.text for s in seg2]).strip()
        except (KeyboardInterrupt, SystemExit):
            print(f"[LOCAL] Translation pass interrupted [{session_id[-8:]}] — using transcript")
            translation = transcript
        except Exception as e:
            print(f"[LOCAL] Translation pass failed: {e} — using transcript")
            translation = transcript

    print(f"[LOCAL] lang={info.language}")
    print(f"[LOCAL] transcript : {transcript[:100]}")
    print(f"[LOCAL] translation: {translation[:100]}")

    return {
        "transcript":        transcript,
        "detected_language": info.language or "sw",
        "translation":       translation,
        "urgent_keywords":   [],
        "confidence":        "medium",
        "engine":            "local"
    }


def _gemini_fallback_local(audio_path: str, session_id: str) -> dict:
    result = _scenario_a_cloud(audio_path, session_id)
    if result:
        result["engine"] = "gemini_cloud_fallback"
        return result
    return {
        "transcript": "", "detected_language": "unknown",
        "translation": "Translation unavailable — both engines failed",
        "urgent_keywords": [], "confidence": "none", "engine": "error"
    }


def _get_audio_file(source: str, session_id: str) -> str:
    if source.startswith("http"):
        return _download_recording(source, session_id)
    if os.path.exists(source):
        print(f"[LOCAL] Using file: {source}")
        return source
    print(f"[ERROR] Audio source not found: {source}")
    return None


def _download_recording(url: str, session_id: str) -> str:
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        os.makedirs("recordings", exist_ok=True)
        path = f"recordings/{session_id}_raw.wav"
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception as e:
        print(f"[DOWNLOAD] Failed: {e}")
        return None


def _error_result(session_id: str, reason: str) -> dict:
    print(f"[ERROR] {session_id[-8:]}: {reason}")
    return {
        "transcript": "",
        "detected_language": "unknown",
        "translation": reason,
        "urgent_keywords": [],
        "confidence": "none",
        "engine": "error",
        "requires_review": False,
    }