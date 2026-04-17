# webhook.py — King'olik NGO System Webhook Bridge
# Pushes translated reports to external NGO tools:
# KoboToolbox, ActivityInfo, Salesforce, custom webhooks.
# Set WEBHOOK_URL in .env to activate.

import os, json, requests

WEBHOOK_URL     = os.environ.get("WEBHOOK_URL","")
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET","")
WEBHOOK_TIMEOUT = 8


def push_to_ngo_system(session: dict, result: dict):
    """
    Pushes a translated report to the configured NGO webhook.
    Silent no-op if WEBHOOK_URL is not set.
    """
    if not WEBHOOK_URL:
        return  # Not configured — silent skip

    payload = {
        "source":          "kingolik",
        "version":         "1.0",
        "session_id":      session.get("session_id",""),
        "timestamp":       session.get("timestamp",""),
        "language":        result.get("detected_language",""),
        "transcript":      result.get("transcript",""),
        "translation":     result.get("translation",""),
        "urgent_keywords": result.get("urgent_keywords",[]),
        "confidence":      result.get("confidence",""),
        "engine":          result.get("engine",""),
        "requires_review": result.get("requires_review", False),
        # Phone hashed for PII compliance
        "caller_id": __import__("hashlib").sha256(
            session.get("phone","").encode()
        ).hexdigest()[:12]
    }

    headers = {"Content-Type": "application/json"}
    if WEBHOOK_SECRET:
        headers["X-Kingolik-Secret"] = WEBHOOK_SECRET

    try:
        resp = requests.post(
            WEBHOOK_URL, json=payload,
            headers=headers, timeout=WEBHOOK_TIMEOUT
        )
        print(f"[WEBHOOK] Pushed  status={resp.status_code}  url={WEBHOOK_URL[:50]}")
    except requests.exceptions.Timeout:
        print(f"[WEBHOOK] Timeout after {WEBHOOK_TIMEOUT}s — skipped")
    except Exception as e:
        print(f"[WEBHOOK] Failed: {e}")