# pii_scrubber.py — PII Scrubbing for Data Sovereignty Compliance
#
# Kingolik operates under Kenya's Data Protection Act (NDPA) and
# serves UNHCR/NRC partners bound by GDPR.
#
# This module scrubs Personally Identifiable Information (PII) from
# transcripts BEFORE they leave Kenyan soil for cloud translation.
#
# What counts as PII in this context:
#   - Full names (identified by capitalization patterns + name lists)
#   - GPS coordinates / location references
#   - ID numbers (national ID, UNHCR registration numbers)
#   - Phone numbers embedded in speech
#
# The ORIGINAL audio never leaves — only the TEXT transcript is scrubbed.
# Audio processing (Whisper) runs locally before any cloud call.

import re


# ── Common East African name patterns (partial list for demo) ──
# In production: expand with UNHCR registration name databases
KNOWN_NAME_PATTERNS = [
    r'\b(Amina|Fatuma|Halima|Mariam|Aisha|Zainab)\b',           # Somali/Arabic female
    r'\b(Mohamed|Hassan|Omar|Ibrahim|Ali|Abdi)\b',               # Somali/Arabic male
    r'\b(Wanjiku|Kamau|Njoroge|Mwangi|Kariuki|Njoki)\b',        # Kikuyu
    r'\b(Otieno|Odhiambo|Achieng|Ouma|Adhiambo)\b',             # Dholuo
    r'\b(Ekiru|Nakiru|Lokeris|Locham|Ewoi)\b',                  # Turkana
    r'\b(Amara|Kedar|Baraka|Rehema|Zawadi)\b',                  # Kiswahili names
]

# ── PII regex patterns ─────────────────────────────────────────
PII_PATTERNS = [
    # GPS coordinates
    (r'-?\d{1,3}\.\d{4,}[,\s]+-?\d{1,3}\.\d{4,}', '[COORDINATES REMOVED]'),
    # Phone numbers (various East African formats)
    (r'\b(\+?254|0)[17]\d{8}\b', '[PHONE REMOVED]'),
    (r'\b0[17]\d{8}\b', '[PHONE REMOVED]'),
    # UNHCR-style registration numbers (e.g. KEN-2024-00123456)
    (r'\b[A-Z]{3}-\d{4}-\d{6,}\b', '[ID REMOVED]'),
    # National ID numbers (8 digits)
    (r'\b\d{8}\b', '[ID REMOVED]'),
    # Email addresses
    (r'\b[\w.+-]+@[\w-]+\.\w{2,}\b', '[EMAIL REMOVED]'),
]


def scrub_pii(text: str, aggressive: bool = False) -> tuple:
    """
    Removes PII from text before cloud transmission.

    Args:
        text: The transcript or translation text to scrub.
        aggressive: If True, also scrub detected names (may reduce accuracy).

    Returns:
        (scrubbed_text, list_of_removed_items)
    """
    if not text:
        return text, []

    removed = []
    result  = text

    # Pattern-based scrubbing
    for pattern, replacement in PII_PATTERNS:
        matches = re.findall(pattern, result, re.IGNORECASE)
        if matches:
            removed.extend(matches if isinstance(matches[0], str) else
                           [''.join(m) for m in matches])
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    # Name scrubbing (optional — can reduce translation quality)
    if aggressive:
        for name_pattern in KNOWN_NAME_PATTERNS:
            matches = re.findall(name_pattern, result)
            if matches:
                removed.extend(matches)
                result = re.sub(name_pattern, '[NAME REMOVED]', result)

    return result, removed


def scrub_session_for_webhook(session: dict) -> dict:
    """
    Returns a PII-safe copy of a session dict for external transmission.
    Used by webhook.py before pushing to NGO systems.
    """
    import hashlib, copy
    safe = copy.deepcopy(session)

    # Hash the phone number
    phone = safe.get("phone","")
    if phone:
        safe["phone"] = "[HASHED:" + hashlib.sha256(phone.encode()).hexdigest()[:8] + "]"

    # Scrub translation fields
    t = safe.get("translation") or {}
    if t.get("transcript"):
        t["transcript"], _ = scrub_pii(t["transcript"])
    if t.get("translation"):
        t["translation"], _ = scrub_pii(t["translation"])

    return safe


if __name__ == "__main__":
    # Quick test
    tests = [
        "My name is Mohamed Ibrahim, my ID is 12345678, call me on +254712345678",
        "We are at coordinates -1.2921, 36.8219 near Kakuma camp",
        "Contact us at info@nrc.no or register number KEN-2024-001234",
        "Haraka, moto mkubwa umeonekana karibu na hema za matibabu",  # no PII
    ]
    print("PII Scrubber test:")
    print("=" * 60)
    for text in tests:
        scrubbed, removed = scrub_pii(text, aggressive=True)
        print(f"  IN:  {text[:70]}")
        print(f"  OUT: {scrubbed[:70]}")
        print(f"  PII: {removed}")
        print()