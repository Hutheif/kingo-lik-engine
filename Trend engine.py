# trend_engine.py — King'olik Trend Detection Engine
# Detects if 3+ sessions mention the same crisis keyword within 4 hours
# then fires an SMS alert to the logistics coordinator.

import os, datetime
from collections import defaultdict

_trend_memory: dict = defaultdict(list)  # keyword → [timestamps]

TREND_CLUSTERS = {
    "water_crisis":   ["maji","water","akwap","drought","dry well","mam akwap","ngakipi"],
    "food_crisis":    ["chakula","food","njaa","hunger","famine","kech","gaajo"],
    "medical":        ["mgonjwa","sick","daktari","hospital","jeraha","injury","damu","blood"],
    "security":       ["vita","violence","shambulio","attack","weerar","gunshot","bunduki"],
    "fire":           ["moto","fire","inawaka","mac","dab","mach"],
    "missing_person": ["kupotea","missing","mafuriko","lunaystay","mtoto","child"],
}

CLUSTER_THRESHOLD = 3
CLUSTER_WINDOW_H  = 4


def check_trends(session: dict, result: dict) -> list:
    """
    Called after each translation.
    Returns list of trend alert dicts if any cluster threshold is hit.
    """
    text = " ".join(filter(None, [
        result.get("translation",""),
        result.get("transcript",""),
        " ".join(result.get("urgent_keywords") or [])
    ])).lower()

    now     = datetime.datetime.utcnow()
    cutoff  = now - datetime.timedelta(hours=CLUSTER_WINDOW_H)
    alerts  = []

    for cluster_name, keywords in TREND_CLUSTERS.items():
        if any(kw in text for kw in keywords):
            # Add this session's timestamp to the cluster
            _trend_memory[cluster_name].append({
                "ts":    now,
                "phone": session.get("phone","?"),
                "sid":   session.get("session_id","?")[-8:]
            })

        # Prune old entries
        _trend_memory[cluster_name] = [
            e for e in _trend_memory[cluster_name]
            if e["ts"] >= cutoff
        ]

        count = len(_trend_memory[cluster_name])
        if count >= CLUSTER_THRESHOLD:
            phones = list(set(e["phone"] for e in _trend_memory[cluster_name]))
            alerts.append({
                "cluster":   cluster_name,
                "count":     count,
                "phones":    phones[:3],
                "message":   f"TREND ALERT: {cluster_name.replace('_',' ').upper()} — {count} reports in {CLUSTER_WINDOW_H}h from {', '.join(phones[:3])}"
            })
            # Reset to avoid repeated alerts for same cluster
            _trend_memory[cluster_name] = []

    return alerts


def send_trend_sms(alerts: list):
    """Sends logistics SMS for each trend alert."""
    coordinator = os.environ.get("LOGISTICS_PHONE") or os.environ.get("DUTY_OFFICER_PHONE","")
    if not coordinator:
        print("[TREND] No LOGISTICS_PHONE set — alert not sent")
        return

    try:
        import africastalking
        sms = africastalking.SMS
        for alert in alerts:
            msg = (
                f"KINGOLIK TREND ALERT\n"
                f"Crisis: {alert['cluster'].replace('_',' ')}\n"
                f"Reports: {alert['count']} in last {CLUSTER_WINDOW_H}h\n"
                f"Callers: {', '.join(alert['phones'])}\n"
                f"Action: Dispatch resources immediately"
            )
            resp = sms.send(message=msg, recipients=[coordinator])
            print(f"[TREND] SMS sent  cluster={alert['cluster']}  resp={resp}")
    except Exception as e:
        print(f"[TREND] SMS failed: {e}")


def send_feedback_to_caller(session_id: str, phone: str, eta_hours: int = 2):
    """
    Sends confirmation SMS to original caller when case is marked as handled.
    Called from database.mark_handled().
    """
    if not phone or phone in ("unknown","local-test",""):
        return

    try:
        import africastalking
        sms = africastalking.SMS
        msg = (
            f"King'olik: Ombi lako limepokelewa.\n"
            f"Msaada unakuja. Muda wa kuwasili: saa {eta_hours}.\n"
            f"Your report was received. Help is on the way. ETA: {eta_hours} hours.\n"
            f"Ref: {session_id[-8:]}"
        )
        resp = sms.send(message=msg, recipients=[phone])
        print(f"[FEEDBACK] SMS sent to {phone}  resp={resp}")
    except Exception as e:
        print(f"[FEEDBACK] SMS failed: {e}")