# trend_engine.py — Field Pilot Logistics Engine
#
# Inspired by aviation dispatch: predict problems before they become crises.
# If 3 people mention "dry wells" in 4 hours from the same area, trigger alert.
# This is what separates King'olik from a recording system.

import os, json, threading, datetime
from collections import defaultdict

# ── Trend thresholds ───────────────────────────────────────────
TREND_WINDOW_HOURS = 4      # look back this many hours
TREND_THRESHOLD    = 3      # this many reports triggers an alert
FEEDBACK_DELAY_SEC = 5      # seconds before sending "help is on the way" SMS

# ── Trend topic clusters ───────────────────────────────────────
# Keywords that belong to the same logistics problem
TREND_CLUSTERS = {
    "water_crisis": [
        "water","maji","ngakipi","biyo","drought","ukame","dry","well","borehole",
        "thirst","no water","maji hakuna","biyo ma jiro","ngadikipi"
    ],
    "food_shortage": [
        "food","chakula","njaa","hunger","famine","starvation","kimuj",
        "starving","no food","chakula hakuna","gaajo","adyokiring"
    ],
    "security_threat": [
        "attack","violence","armed","soldiers","militia","gun","bunduki",
        "shambulio","weerar","raid","shooting","danger","hatari","khatar"
    ],
    "medical_emergency": [
        "injured","bleeding","damu","jeraha","hospital","hospitali","doctor",
        "daktari","nabar","dead","dying","unconscious","ambulance","sick"
    ],
    "fire": [
        "fire","moto","mach","dab","flames","burning","inawaka","mwaki",
        "haraka","big fire","moto mkubwa","mach maduong"
    ],
    "displacement": [
        "displaced","flood","mafuriko","collapsed","homeless","tent","hema",
        "shelter","makazi","no shelter","barakacay","qaxooti"
    ]
}


def check_trends(new_session: dict, new_result: dict) -> list:
    """
    Called after every translation. Checks if this report creates a trend.
    Returns list of alert dicts if thresholds are exceeded.
    """
    alerts = []
    try:
        from database import get_all_sessions
        sessions  = get_all_sessions()
        cutoff    = datetime.datetime.utcnow() - datetime.timedelta(hours=TREND_WINDOW_HOURS)
        recent    = []
        for s in sessions:
            try:
                if datetime.datetime.fromisoformat(s.get("timestamp","")) >= cutoff:
                    recent.append(s)
            except Exception:
                pass

        # Count keyword hits per cluster across recent sessions
        cluster_hits = defaultdict(list)
        for s in recent:
            t    = s.get("translation") or {}
            text = " ".join(filter(None, [
                t.get("translation",""), t.get("transcript",""),
                " ".join(t.get("urgent_keywords") or [])
            ])).lower()
            for cluster, keywords in TREND_CLUSTERS.items():
                for kw in keywords:
                    if kw in text:
                        cluster_hits[cluster].append(s.get("session_id",""))
                        break  # one hit per session per cluster

        # Check thresholds
        for cluster, session_ids in cluster_hits.items():
            unique_sessions = list(set(session_ids))
            if len(unique_sessions) >= TREND_THRESHOLD:
                alert = {
                    "type":          "trend_alert",
                    "cluster":       cluster,
                    "count":         len(unique_sessions),
                    "window_hours":  TREND_WINDOW_HOURS,
                    "threshold":     TREND_THRESHOLD,
                    "triggered_at":  datetime.datetime.utcnow().isoformat(),
                    "sessions":      unique_sessions[-5:],  # last 5 session IDs
                    "message":       _format_alert_message(cluster, len(unique_sessions))
                }
                alerts.append(alert)
                print(f"[TREND] ALERT: {cluster} — {len(unique_sessions)} reports "
                      f"in {TREND_WINDOW_HOURS}h (threshold: {TREND_THRESHOLD})")

    except Exception as e:
        print(f"[TREND] Check failed: {e}")

    return alerts


def _format_alert_message(cluster: str, count: int) -> str:
    messages = {
        "water_crisis":      f"LOGISTICS ALERT: {count} reports of water shortage in {TREND_WINDOW_HOURS}h. Dispatch water truck.",
        "food_shortage":     f"LOGISTICS ALERT: {count} reports of food shortage in {TREND_WINDOW_HOURS}h. Notify food distribution.",
        "security_threat":   f"SECURITY ALERT: {count} security incidents reported in {TREND_WINDOW_HOURS}h. Notify protection cluster.",
        "medical_emergency": f"MEDICAL ALERT: {count} medical emergencies in {TREND_WINDOW_HOURS}h. Deploy health team.",
        "fire":              f"FIRE ALERT: {count} fire reports in {TREND_WINDOW_HOURS}h. Dispatch emergency response.",
        "displacement":      f"DISPLACEMENT ALERT: {count} displacement reports in {TREND_WINDOW_HOURS}h. Notify shelter cluster.",
    }
    return messages.get(cluster, f"TREND ALERT: {count} related reports in {TREND_WINDOW_HOURS}h.")


def send_trend_sms(alerts: list):
    """Sends SMS to alert number for each triggered trend. Runs in background thread."""
    alert_number = os.environ.get("ALERT_PHONE","")
    if not alert_number or not alerts:
        return

    def _send():
        try:
            import africastalking
            sms = africastalking.SMS
            for alert in alerts:
                msg = f"KINGOLIK TREND ALERT\n{alert['message']}\nTime: {alert['triggered_at'][:16]}"
                resp = sms.send(msg, [alert_number], sender_id=None)
                print(f"[TREND SMS] Sent: {alert['cluster']}  resp={resp}")
        except Exception as e:
            print(f"[TREND SMS] Failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


def send_feedback_to_caller(session_id: str, phone: str, eta_hours: int = 2):
    """
    Sends 'help is on the way' SMS back to the original caller
    when a caseworker marks a session as handled.
    This closes the loop and builds trust with the community.
    """
    if not phone or phone in ("unknown", "local-test"):
        return

    def _send():
        try:
            import africastalking
            sms = africastalking.SMS
            message = (
                f"Habari / Hello.\n"
                f"King'olik: Your report has been received and is being handled.\n"
                f"Msaada unakuja. Help is on the way.\n"
                f"Expected within {eta_hours} hours.\n"
                f"Ref: ...{session_id[-6:]}"
            )
            resp = sms.send(message, [phone], sender_id=None)
            recipients = resp.get("SMSMessageData",{}).get("Recipients",[])
            status = recipients[0].get("status","?") if recipients else "?"
            print(f"[FEEDBACK SMS] Sent to caller {phone}  status={status}")
        except Exception as e:
            print(f"[FEEDBACK SMS] Failed: {e}")

    threading.Thread(target=_send, daemon=True).start()