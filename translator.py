# translator.py
import os, re, json, requests, threading, time, shutil
from dotenv import load_dotenv
from audio_processor import process_audio, is_duplicate
from schema_validator import validate_and_normalise

load_dotenv()

from google import genai
from google.genai import types
import os
import re
import json

# Detect if we are running on Render or Railway
IS_CLOUD = os.environ.get("RENDER") or os.environ.get("RAILWAY_ENVIRONMENT")

def _scenario_b_local(audio_path: str, session_id: str) -> dict:
    if IS_CLOUD:
        # Skip the 1.5GB Whisper download on cloud; use Gemini/Groq instead
        return _groq_fallback(audio_path, session_id)
    
    # Your existing Whisper code goes here for local dev
    pass

def _groq_fallback(audio_path: str, session_id: str) -> dict:
    """Cloud fallback: Transcribes and translates using Gemini 2.0 Flash."""
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        
        from google import genai
        from google.genai import types
        
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        
        response = client.models.generate_content(
            model="gemini-2.0-flash", # Note: using 2.0 as 2.5 is not released yet
            contents=[
                types.Content(parts=[
                    types.Part(text='Transcribe and translate to English. Return JSON: {"transcript":"","detected_language":"","translation":"","urgent_keywords":[],"confidence":"medium"}'),
                    types.Part(inline_data=types.Blob(mime_type="audio/wav", data=audio_bytes))
                ])
            ]
        )
        
        raw = re.sub(r"```json|```", "", response.text).strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        return json.loads(match.group() if match else raw)
        
    except Exception as e:
        return {
            "transcript": "", 
            "detected_language": "unknown",
            "translation": f"Cloud transcription failed: {e}",
            "urgent_keywords": [], 
            "confidence": "none"
        }

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

CLOUD_TIMEOUT = 30



# ══════════════════════════════════════════════════════════════
#  Multilingual urgent keyword detector
#  Languages: English, Kiswahili, Turkana, Somali,
#             Arabic, Kikuyu, Dholuo
#  Runs on the final result of BOTH cloud and local engines.
# ══════════════════════════════════════════════════════════════
URGENT_KEYWORDS = {
    # ── English ───────────────────────────────────────────────
    "fire","flames","burning","smoke","explosion",
    "attack","attacked","violence","violent","shooting","shot",
    "gun","knife","weapon","armed","militia","soldiers","raid",
    "bleeding","blood","injured","injury","unconscious","dead",
    "death","dying","hospital","ambulance","doctor","medicine",
    "sick","pain","wound","emergency","urgent","help","sos",
    "danger","crisis","missing","lost","abducted","kidnapped",
    "child","flood","collapsed","homeless","displaced","hunger",
    "starving","starvation","famine","drought","thirst",

    # ── Kiswahili ─────────────────────────────────────────────
    "moto","inawaka","mwako","haraka","msaada","dharura",
    "hatari","shambulio","vita","kupigwa","bunduki","kisu",
    "damu","jeraha","kuumia","daktari","hospitali","dawa",
    "mgonjwa","ugonjwa","kufa","maiti","njaa","maji","ukame",
    "chakula","mtoto","kupotea","kutekwa","mafuriko","hema",
    "makazi","saidia","omba",

    # ── Turkana ───────────────────────────────────────────────
    "akuj","edome","apese","ngikamatak","ngikaalon",
    "aberu","ngosi","ekitoi","anam","lokwae","erot",
    "ngikairiamit","ekisil","emuron","abakare","ngiyapese",
    "ngikasit","lokale","ngikaabong","tukoi","apei",
    "ngikoriyang","ekurukan","ngiyetu",

    # ── Somali ────────────────────────────────────────────────
    "dab","gubashada","hubaal","weerar","xoog","rabshad",
    "dhiig","nabar","xanuun","dhakhtarka","isbitaalka",
    "dawo","buka","geerida","baahi","caafimaad",
    "biyo","gaajo","abaar","carruur","lunaystay",
    "khatar","gargaar","degdeg","colaad","qori",
    "argagax","qaxooti","barakacay","nabadgelyo",

    # ── Arabic ────────────────────────────────────────────────
    "نار","حريق","مساعدة","طوارئ","خطر","هجوم","جرح",
    "دم","مستشفى","ماء","جوع","مفقود","فيضان","عنف",
    "nar","hariq","musaada","tawari","khatar","hujum",
    "jurh","dam","mustashfa","maa","juu","mafqud",

    # ── Kikuyu ────────────────────────────────────────────────
    "mwaki","thimu","ndeto","ota","mũrũ","ndũrire",
    "thahu","mũndũ","gũkuĩra","mũganga","ndawa","mũirutwo",
    "nĩ ũhoro","tiga","njenga","ũhiu","maaĩ","gũtĩĩka",
    "mwĩrĩ","ndiri maaĩ","mũtũme","gũcooka",

    # ── Dholuo ────────────────────────────────────────────────
    "mac","mach","kony","tuo","remo","ndiko",
    "japuonj","yath","oganda","lamo","kech","pi",
    "ndala","tho","luoro","owuok","rach","siro",
}

URGENT_PHRASES = [
    # English
    "medical tent","medical tents","no food","no water",
    "people dying","need help","send help","large fire",
    "big fire","brush fire","near the camp","under attack",
    "gun shot","knife attack","missing child","flash flood",
    # Kiswahili
    "hema za matibabu","moto mkubwa","maji hakuna",
    "chakula hakuna","watu wanakufa","tuma msaada",
    "karibu na kambi","moto wa msituni","mtoto amepotea",
    "mafuriko makubwa","msaada wa haraka",
    # Somali
    "caafimaad ma jiro","biyo ma jiro","gargaar deg deg",
    "carruurta lunaystay","weerar waa socda",
    # Arabic
    "لا ماء","لا طعام","مساعدة عاجلة","حريق كبير",
    # Dholuo
    "kony koro","mach maduong","pi onge","kech malit",
    # Turkana
    "apese ngosi","tukoi lokwae",
]


def detect_urgent_keywords(text: str) -> list:
    """
    Scans translation + transcript for urgent keywords and phrases.
    Works on any of the 7 supported languages.
    Returns a sorted, deduplicated list of matched terms.
    """
    if not text:
        return []
    text_lower = text.lower()
    found = set()
    # Single word matches — includes Arabic/Unicode characters
    words = re.findall(r'[\w\u0600-\u06FF]+', text_lower)
    for word in words:
        if word in URGENT_KEYWORDS:
            found.add(word)
    # Multi-word phrase matches
    for phrase in URGENT_PHRASES:
        if phrase.lower() in text_lower:
            found.add(phrase)
    return sorted(found)


# ══════════════════════════════════════════════════════════════
#  Whisper model cache
#  medium model — significantly better than small for all
#  East African languages (Kiswahili, Somali, Dholuo, Turkana)
# ══════════════════════════════════════════════════════════════
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
                    "medium", device="cpu", compute_type="int8"
                )
                print("[LOCAL] Whisper medium ready")
    return _whisper_model


# ══════════════════════════════════════════════════════════════
#  Gemini prompt — explicit field definitions + urgency context
# ══════════════════════════════════════════════════════════════
# ── Load Turkana grammar rules for RAG injection ─────────────
def _load_turkana_rules() -> str:
    """Loads Turkana grammar rules from JSON for RAG injection into Gemini."""
    try:
        import json
        rules_path = os.path.join(os.path.dirname(__file__), "turkana_rules.json")
        if os.path.exists(rules_path):
            rules = json.load(open(rules_path))
            # Extract the most critical rules for prompt injection
            return rules.get("gemini_injection_prompt", "")
    except Exception:
        pass
    return ""

_TURKANA_RAG = _load_turkana_rules()

GEMINI_PROMPT = """You are a humanitarian AI assistant for an NGO in remote East Africa.
A community member has left a voice message. Transcribe and translate it accurately.

Return ONLY a valid JSON object with exactly these fields:
{
  "transcript": "exact words in the original language",
  "detected_language": "ISO code: sw en so ar ki luo tuk",
  "translation": "accurate English translation",
  "urgent_keywords": ["any urgent words or phrases found"],
  "confidence": "high or medium or low"
}

Mark urgent_keywords if the message mentions ANY of:
- Fire, burning, explosion (moto, dab, حريق, mach, mwaki)
- Violence, attack, weapons (shambulio, weerar, هجوم, kony)
- Medical emergency, injury, death (jeraha, nabar, جرح, tuo)
- Missing persons, abduction (kupotea, lunaystay, مفقود)
- Water or food shortage (maji, biyo, ماء, pi, njaa, gaajo)
- Distress calls (msaada, gargaar, مساعدة, kony, haraka, help)

Accuracy is critical. Lives depend on this translation.

TURKANA-SPECIFIC RULES (apply when detected_language appears to be Turkana/tuk/ng):
{turkana_rag}

Do not add any text outside the JSON object.""".format(turkana_rag=_TURKANA_RAG)


# ══════════════════════════════════════════════════════════════
#  Main pipeline
# ══════════════════════════════════════════════════════════════
def process_recording(session_id: str, audio_source: str,
                      phone_number: str = "unknown") -> dict:

    print(f"\n[TRANSLATE] Session: {session_id[-8:]}")
    print(f"[TRACE] source={audio_source}")

    # Step 1 — get audio
    raw_path = _get_audio_file(audio_source, session_id)
    if not raw_path:
        return _error_result(session_id, "Audio source invalid")

    # Step 2 — duplicate check
    if is_duplicate(raw_path, session_id):
        return _error_result(session_id, "Duplicate audio skipped")

    # Step 3 — clean audio
    try:
        clean_path = process_audio(raw_path, session_id)
    except Exception as e:
        print(f"[AUDIO] Cleaning failed: {e} — using raw")
        clean_path = raw_path

    # Step 4 — save session copy for audio player
    os.makedirs("recordings", exist_ok=True)
    session_copy = os.path.join(
        os.getcwd(), "recordings", f"{session_id}_raw_clean.wav"
    )
    if not os.path.exists(session_copy):
        source_for_copy = clean_path if os.path.exists(clean_path) else raw_path
        try:
            shutil.copy2(source_for_copy, session_copy)
            print(f"[AUDIO] Saved → {session_id[-8:]}_raw_clean.wav")
        except Exception as e:
            print(f"[AUDIO] Copy failed: {e}")

    # Step 5 — translation (with latency tracking)
    _t0 = time.time()
    try:
        from hybrid_engine import translate_with_confidence
        result = translate_with_confidence(
            audio_path=clean_path,
            session_id=session_id,
            cloud_fn=lambda p, s: _scenario_a_cloud(raw_path, s),
            local_fn=_scenario_b_local
        )
        latency_ms = int((time.time() - _t0) * 1000)
        result["latency_ms"] = latency_ms
        print(f"[TRANSLATE] Engine={result.get('engine','?').upper()} "
              f"Score={result.get('score','n/a')} Latency={latency_ms}ms")
    except ImportError:
        print("[TRANSLATE] No hybrid_engine — direct fallback")
        try:
            raw_result = _scenario_a_cloud(raw_path, session_id)
            result = validate_and_normalise(raw_result, engine="cloud")
        except Exception as e:
            print(f"[FALLBACK] Cloud failed: {e} — local")
            raw_result = _scenario_b_local(clean_path, session_id)
            result = validate_and_normalise(raw_result, engine="local")
    except Exception as e:
        print(f"[TRANSLATE] All engines failed: {e}")
        result = _error_result(session_id, str(e))

    # Step 6 — keyword detection on final result (both engines)
    combined = " ".join(filter(None, [
        result.get("translation", ""),
        result.get("transcript", "")
    ]))
    detected = detect_urgent_keywords(combined)
    existing = result.get("urgent_keywords") or []
    merged   = sorted(set(existing) | set(detected))
    result["urgent_keywords"] = merged

    if merged:
        print(f"[URGENT] Detected: {merged}")
    else:
        print("[URGENT] No urgent keywords")

    # ── Confidence gate ───────────────────────────────────────
    # If confidence is low, flag for human review instead of
    # passing a potentially wrong translation to a caseworker.
    CONFIDENCE_THRESHOLD = 0.85   # 85% — matches pitch deck claim
    confidence_str = result.get("confidence","medium")
    conf_map = {"high": 1.0, "medium": 0.75, "low": 0.40, "none": 0.0}
    conf_score = conf_map.get(confidence_str, 0.75)

    if conf_score < CONFIDENCE_THRESHOLD:
        result["requires_review"] = True
        result["review_reason"]   = (
            f"Confidence {confidence_str} ({int(conf_score*100)}%) "
            f"below {int(CONFIDENCE_THRESHOLD*100)}% threshold"
        )
        print(f"[GATE] UNCERTAIN — flagged for human review "
              f"(confidence={confidence_str})")
    else:
        result["requires_review"] = False

    # Step 7 — save
    print(f"[DEBUG] Translation: {result.get('translation','')[:100]}")
    try:
        from database import save_translation
        save_translation(session_id, result)
    except Exception as e:
        print(f"[DB] Save failed: {e}")

    return result


# ══════════════════════════════════════════════════════════════
#  Engines
# ══════════════════════════════════════════════════════════════
def _scenario_a_cloud(audio_path: str, session_id: str) -> dict:
    result_box = [None]
    error_box  = [None]

    def call():
        try:
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()
            response = client.models.generate_content(
                model="models/gemini-2.5-flash",
                contents=[types.Content(parts=[
                    types.Part(text=GEMINI_PROMPT),
                    types.Part(inline_data=types.Blob(
                        mime_type="audio/wav",
                        data=audio_bytes
                    ))
                ])]
            )
            raw = re.sub(r"```json|```", "", response.text.strip()).strip()
            result_box[0] = json.loads(raw)
        except Exception as e:
            error_box[0] = e

    t = threading.Thread(target=call)
    t.start()
    t.join(timeout=CLOUD_TIMEOUT)

    if t.is_alive():
        raise TimeoutError("Cloud timeout after 30s")
    if error_box[0]:
        raise error_box[0]
    return result_box[0]


def _scenario_b_local(audio_path: str, session_id: str) -> dict:
    model = _get_whisper_model()

    segments, info = model.transcribe(audio_path, task="transcribe")
    transcript = " ".join([s.text for s in segments]).strip()

    if info.language and info.language.lower() != "en":
        seg2, _ = model.transcribe(audio_path, task="translate")
        translation = " ".join([s.text for s in seg2]).strip()
    else:
        translation = transcript

    print(f"[LOCAL] lang={info.language}")
    print(f"[LOCAL] transcript : {transcript[:100]}")
    print(f"[LOCAL] translation: {translation[:100]}")

    return {
        "transcript":        transcript,
        "detected_language": info.language,
        "translation":       translation,
        "urgent_keywords":   [],   # filled by detect_urgent_keywords in Step 6
        "confidence":        "medium",
        "engine":            "local"
    }


def _get_audio_file(source: str, session_id: str) -> str:
    if source.startswith("http"):
        return _download_recording(source, session_id)
    if os.path.exists(source):
        print(f"[LOCAL] Using file: {source}")
        return source
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
        "transcript":        "",
        "detected_language": "unknown",
        "translation":       reason,
        "urgent_keywords":   [],
        "confidence":        "none",
        "engine":            "error"
    }