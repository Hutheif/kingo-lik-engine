# webhook.py — Open API / Interoperability Bridge
# Pushes translated reports to external NGO systems automatically.
# Supports: any HTTP endpoint (KoboToolbox, ActivityInfo, Salesforce, custom)
#
# Usage:
#   Set WEBHOOK_URL in .env to your NGO's intake endpoint.
#   When a translation is saved, Kingolik POSTs a JSON payload to that URL.
#   The NGO's system receives structured data without manual copy-paste.
#
# Example .env:
#   WEBHOOK_URL=https://kobo.humanitarianresponse.info/api/v2/assets/YOUR_ASSET/submissions/
#   WEBHOOK_TOKEN=your_api_token_here

import os, json, requests, threading, datetime
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL   = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")


def push_to_ngo_system(session: dict, result: dict):
    """
    Pushes a translated report to the NGO's external system.
    Fires in a background thread — never blocks the main pipeline.
    If WEBHOOK_URL is not set, this is a no-op.
    """
    if not WEBHOOK_URL:
        return  # Webhook not configured — skip silently

    threading.Thread(
        target=_send_webhook,
        args=(session, result),
        daemon=True
    ).start()


def _send_webhook(session: dict, result: dict):
    """
    Builds a standardised JSON payload and POSTs it to the webhook URL.
    Payload follows the Humanitarian Data Exchange (HDX) field naming
    convention so it maps cleanly to KoboToolbox / ActivityInfo schemas.
    """
    try:
        keywords = result.get("urgent_keywords") or []
        payload  = {
            # ── Identity ──────────────────────────────────────
            "source_system":      "kingolik_voice_bridge",
            "session_id":         session.get("session_id",""),
            "submission_time":    datetime.datetime.utcnow().isoformat() + "Z",

            # ── Caller ────────────────────────────────────────
            # Phone number is hashed for PII compliance on transmission
            "caller_phone_hash":  _hash_phone(session.get("phone","")),

            # ── Voice message ─────────────────────────────────
            "original_language":  result.get("detected_language",""),
            "original_transcript": result.get("transcript",""),
            "english_translation": result.get("translation",""),

            # ── Urgency ───────────────────────────────────────
            "is_urgent":          len(keywords) > 0,
            "urgent_keywords":    keywords,
            "requires_review":    result.get("requires_review", False),
            "review_reason":      result.get("review_reason",""),

            # ── Quality metadata ──────────────────────────────
            "confidence":         result.get("confidence",""),
            "engine":             result.get("engine",""),
            "latency_ms":         result.get("latency_ms", 0),

            # ── Caseworker ────────────────────────────────────
            "caseworker_note":    session.get("note",""),
            "status":             session.get("status",""),
        }

        headers = {"Content-Type": "application/json"}
        if WEBHOOK_TOKEN:
            headers["Authorization"] = f"Token {WEBHOOK_TOKEN}"

        resp = requests.post(
            WEBHOOK_URL,
            json=payload,
            headers=headers,
            timeout=10
        )

        if resp.status_code in (200, 201):
            print(f"[WEBHOOK] Pushed to NGO system  status={resp.status_code}")
        else:
            print(f"[WEBHOOK] Failed  status={resp.status_code}  "
                  f"body={resp.text[:100]}")

    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")


def _hash_phone(phone: str) -> str:
    """One-way hash of phone number for PII-safe transmission."""
    import hashlib
    return hashlib.sha256(phone.encode()).hexdigest()[:16] if phone else ""