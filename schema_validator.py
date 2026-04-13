# schema_validator.py
"""
Ensures cloud (Gemini) and local (Whisper) always return
identical JSON structure regardless of what the AI decided to call things.
"""

URGENT_KEYWORDS_MASTER = [
    # English
    "hospital","doctor","sick","dead","dying","bleeding","hurt","injured",
    "help","emergency","fire","flood","missing","lost","child","baby",
    "food","water","hungry","starving","attack","violence","police",
    "arrested","shelter","medical","health","wound","burn","rape","threat",
    # Swahili
    "hospitali","daktari","mgonjwa","msaada","dharura","moto","mafuriko",
    "mtoto","njaa","maji","vita","polisi","jeraha","chakula","hatari",
    # Turkana / Somali approximations
    "biyo","caafimaad","gargaar","xanuun"
]

def validate_and_normalise(raw: dict, engine: str = "unknown") -> dict:
    """
    Takes raw AI output and returns a guaranteed-consistent schema.
    No matter what keys the AI used, the output always has the same structure.
    """
    # Handle all possible key name variations from different engines
    transcript = (
        raw.get("transcript") or
        raw.get("transcription") or
        raw.get("original_text") or
        raw.get("text") or ""
    )

    translation = (
        raw.get("translation") or
        raw.get("english_translation") or
        raw.get("translated_text") or
        raw.get("english") or
        transcript  # fallback: if no translation, use transcript
    )

    detected_language = (
        raw.get("detected_language") or
        raw.get("language") or
        raw.get("lang") or
        "unknown"
    )

    confidence = (
        raw.get("confidence") or
        raw.get("confidence_score") or
        "medium"
    )

    # Normalise confidence to always be high/medium/low string
    if isinstance(confidence, float):
        confidence = "high" if confidence > 0.8 else "medium" if confidence > 0.5 else "low"

    # Scan for urgent keywords across both transcript and translation
    combined_text = f"{transcript} {translation}".lower()
    found_keywords = list(set(
        kw for kw in URGENT_KEYWORDS_MASTER
        if kw.lower() in combined_text
    ))

    # Also keep any keywords the AI explicitly flagged
    ai_keywords = raw.get("urgent_keywords") or raw.get("keywords") or []
    if isinstance(ai_keywords, list):
        found_keywords = list(set(found_keywords + ai_keywords))

    # Final normalised schema — dashboard always gets this exact structure
    return {
        "transcript":         transcript,
        "detected_language":  detected_language,
        "translation":        translation,
        "urgent_keywords":    found_keywords,
        "confidence":         confidence,
        "engine":             engine,
        "schema_version":     "1.0"
    }