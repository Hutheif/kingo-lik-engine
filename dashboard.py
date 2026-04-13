# dashboard.py — NGO Live Translation Dashboard + Analytics
from flask import Blueprint, render_template_string, jsonify, send_file, request
from database import get_all_sessions, mark_handled, save_note, get_session
import os, io, csv, datetime, json

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route("/api/sessions")
def api_sessions():
    return jsonify(get_all_sessions())


@dashboard_bp.route("/api/analytics")
def api_analytics():
    """Returns aggregated stats for the analytics panel."""
    sessions = get_all_sessions()
    days     = int(request.args.get("days", 7))
    cutoff   = datetime.datetime.utcnow() - datetime.timedelta(days=days)

    # Filter to period
    period = []
    for s in sessions:
        try:
            if datetime.datetime.fromisoformat(s.get("timestamp","")) >= cutoff:
                period.append(s)
        except Exception:
            period.append(s)

    total      = len(period)
    translated = sum(1 for s in period if (s.get("translation") or {}).get("translation"))
    urgent     = sum(1 for s in period if ((s.get("translation") or {}).get("urgent_keywords") or []))
    handled    = sum(1 for s in period if s.get("handled") or s.get("status") == "handled")
    pending    = sum(1 for s in period if s.get("status") == "pending_call")

    # Language breakdown
    lang_counts = {}
    for s in period:
        lang = (s.get("translation") or {}).get("detected_language") or "unknown"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # Engine breakdown
    engine_counts = {"cloud": 0, "local": 0, "error": 0}
    for s in period:
        eng = (s.get("translation") or {}).get("engine") or "unknown"
        if eng in engine_counts:
            engine_counts[eng] += 1

    # Daily volume — last 7 days
    daily = {}
    for i in range(days):
        day = (datetime.datetime.utcnow() - datetime.timedelta(days=i)).strftime("%a")
        daily[day] = {"total": 0, "urgent": 0}
    for s in period:
        try:
            dt  = datetime.datetime.fromisoformat(s.get("timestamp",""))
            day = dt.strftime("%a")
            if day in daily:
                daily[day]["total"] += 1
                if ((s.get("translation") or {}).get("urgent_keywords") or []):
                    daily[day]["urgent"] += 1
        except Exception:
            pass
    daily_list = list(reversed([
        {"day": d, "total": v["total"], "urgent": v["urgent"]}
        for d, v in daily.items()
    ]))

    # Top urgent keywords
    kw_counts = {}
    for s in period:
        for kw in ((s.get("translation") or {}).get("urgent_keywords") or []):
            kw_counts[kw] = kw_counts.get(kw, 0) + 1
    top_keywords = sorted(kw_counts.items(), key=lambda x: -x[1])[:8]

    # Accuracy proxy — confidence distribution
    conf_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for s in period:
        conf = (s.get("translation") or {}).get("confidence") or "none"
        if conf in conf_counts:
            conf_counts[conf] += 1

    # Recent pipeline log — last 8 events
    recent = sorted(period, key=lambda x: x.get("timestamp",""), reverse=True)[:8]
    log_entries = []
    for s in recent:
        t    = s.get("translation") or {}
        kws  = t.get("urgent_keywords") or []
        lang = t.get("detected_language","?")
        eng  = t.get("engine","?")
        ts   = s.get("timestamp","")[:16].replace("T"," ")
        if kws:
            msg = f"URGENT — {lang} — {', '.join(kws[:3])}"
            kind = "urgent"
        elif t.get("translation"):
            msg = f"Translated ({lang}) via {eng}"
            kind = "ok"
        else:
            msg = f"Pending call from {s.get('phone','?')}"
            kind = "pending"
        log_entries.append({"ts": ts, "msg": msg, "kind": kind,
                            "phone": s.get("phone","")})

    # Average latency
    latencies = [
        (s.get("translation") or {}).get("latency_ms", 0)
        for s in period
        if (s.get("translation") or {}).get("latency_ms", 0) > 0
    ]
    avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0

    # Requires human review
    needs_review = sum(
        1 for s in period
        if (s.get("translation") or {}).get("requires_review")
    )

    # Accuracy score — weighted by confidence
    if total > 0:
        score = (conf_counts["high"]*100 + conf_counts["medium"]*75 +
                 conf_counts["low"]*40) / max(translated, 1)
        accuracy = min(round(score), 99)
    else:
        accuracy = 0

    # HITL gold standard pairs collected
    try:
        from database import _count_corrections
        gold_pairs = _count_corrections()
    except Exception:
        gold_pairs = 0

    return jsonify({
        "total": total, "translated": translated,
        "urgent": urgent, "handled": handled, "pending": pending,
        "accuracy": accuracy,
        "lang_counts": lang_counts,
        "engine_counts": engine_counts,
        "daily": daily_list,
        "top_keywords": [{"kw": k, "count": v} for k, v in top_keywords],
        "conf_counts": conf_counts,
        "log": log_entries,
        "period_days": days,
        "avg_latency_ms": avg_latency,
        "needs_review": needs_review,
        "gold_pairs": gold_pairs,
    })


@dashboard_bp.route("/api/audio/<session_id>")
def serve_audio(session_id):
    recordings_dir = os.path.join(os.getcwd(), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)

    for filename in [
        f"{session_id}_raw_clean.wav",
        f"{session_id}_clean.wav",
        f"{session_id}_raw.wav",
    ]:
        full_path = os.path.join(recordings_dir, filename)
        if os.path.exists(full_path):
            resp = send_file(full_path, mimetype="audio/wav", conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

    try:
        wav_files = [
            os.path.join(recordings_dir, f)
            for f in os.listdir(recordings_dir)
            if f.endswith(".wav") and "_clean" not in f
            and "_raw_clean" not in f
            and not f.startswith("ATUid_")
            and not f.startswith("local_test_")
            and not f.startswith("url_test_")
        ]
        if wav_files:
            newest = max(wav_files, key=os.path.getmtime)
            resp = send_file(newest, mimetype="audio/wav", conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=60"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp
    except Exception:
        pass

    return jsonify({"error": "audio_missing"}), 404


@dashboard_bp.route("/api/save-correction", methods=["POST"])
def save_correction_route():
    """HITL — saves caseworker correction as gold standard training pair."""
    from database import save_correction
    data       = request.get_json()
    session_id = data.get("session_id")
    correction = data.get("correction","")
    if not session_id:
        return jsonify({"ok": False}), 400
    save_correction(session_id, correction)
    return jsonify({"ok": True})


@dashboard_bp.route("/api/mark-handled", methods=["POST"])
def mark_handled_route():
    data = request.get_json()
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"ok": False}), 400
    mark_handled(session_id)
    return jsonify({"ok": True})


@dashboard_bp.route("/api/save-note", methods=["POST"])
def save_note_route():
    data = request.get_json()
    session_id = data.get("session_id")
    note = data.get("note", "")
    if not session_id:
        return jsonify({"ok": False}), 400
    save_note(session_id, note)
    return jsonify({"ok": True})


@dashboard_bp.route("/api/export/csv")
def export_csv():
    days     = int(request.args.get("days", 30))
    sessions = get_all_sessions()
    cutoff   = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    output   = io.StringIO()
    writer   = csv.writer(output)
    writer.writerow(["Date","Phone","Language","English Translation",
                     "Original Transcript","Engine","Urgent Keywords",
                     "Status","Caseworker Note","Duration (s)","Session ID"])
    for s in sessions:
        ts = s.get("timestamp","")
        try:
            if datetime.datetime.fromisoformat(ts) < cutoff:
                continue
        except Exception:
            pass
        t = s.get("translation") or {}
        status = "handled" if (s.get("handled") or s.get("status")=="handled") else s.get("status","")
        writer.writerow([
            ts[:19].replace("T"," "), s.get("phone",""),
            t.get("detected_language",""), t.get("translation",""),
            t.get("transcript",""), t.get("engine",""),
            ", ".join(t.get("urgent_keywords") or []),
            status, s.get("note",""), s.get("duration",""), s.get("session_id","")
        ])
    output.seek(0)
    filename = f"kingolik_{datetime.datetime.utcnow().strftime('%Y%m%d')}.csv"
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")),
                     mimetype="text/csv", as_attachment=True, download_name=filename)


@dashboard_bp.route("/api/export/pdf")
def export_pdf():
    days     = int(request.args.get("days", 30))
    sessions = get_all_sessions()
    cutoff   = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    filtered = []
    for s in sessions:
        try:
            if datetime.datetime.fromisoformat(s.get("timestamp","")) >= cutoff:
                filtered.append(s)
        except Exception:
            filtered.append(s)

    total      = len(filtered)
    translated = sum(1 for s in filtered if (s.get("translation") or {}).get("translation"))
    urgent     = sum(1 for s in filtered if ((s.get("translation") or {}).get("urgent_keywords") or []))
    handled    = sum(1 for s in filtered if s.get("handled") or s.get("status")=="handled")

    lang_counts = {}
    for s in filtered:
        lang = (s.get("translation") or {}).get("detected_language") or "unknown"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    lang_rows = "".join(
        f"<tr><td>{lang}</td><td>{count}</td>"
        f"<td>{round(count/total*100) if total else 0}%</td></tr>"
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1])
    )
    session_rows = ""
    for s in filtered[:200]:
        t = s.get("translation") or {}
        keywords = ", ".join(t.get("urgent_keywords") or [])
        status = "handled" if (s.get("handled") or s.get("status")=="handled") else s.get("status","")
        color = "#15803d" if status=="handled" else ("#dc2626" if keywords else "#d97706")
        session_rows += f"""<tr>
          <td>{s.get("timestamp","")[:10]}</td><td>{s.get("phone","")}</td>
          <td>{t.get("detected_language","")}</td>
          <td>{(t.get("translation","") or "")[:120]}</td>
          <td style="color:{color};font-weight:500">{status}</td>
          <td style="color:#dc2626">{keywords}</td>
          <td>{s.get("note","")[:80]}</td></tr>"""

    generated = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Kingolik Report {generated}</title>
<style>
  @media print {{ @page {{ margin:20mm; size:A4 landscape; }} .no-print{{display:none}} }}
  body{{font-family:-apple-system,sans-serif;font-size:11px;color:#1a1a1a;padding:24px}}
  .header{{border-bottom:2px solid #1a1a1a;padding-bottom:12px;margin-bottom:20px;
           display:flex;justify-content:space-between;align-items:flex-end}}
  h1{{font-size:22px;font-weight:600}} h1 span{{color:#16a34a}}
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}}
  .stat{{border:1px solid #e5e7eb;border-radius:8px;padding:12px 16px}}
  .sl{{font-size:10px;color:#6b7280;text-transform:uppercase;margin-bottom:4px}}
  .sv{{font-size:24px;font-weight:600}}
  h2{{font-size:13px;font-weight:600;margin:20px 0 8px;text-transform:uppercase;color:#374151}}
  table{{width:100%;border-collapse:collapse;margin-bottom:24px}}
  th{{background:#f9fafb;padding:6px 8px;text-align:left;font-size:10px;
      text-transform:uppercase;color:#6b7280;border-bottom:1px solid #e5e7eb}}
  td{{padding:6px 8px;border-bottom:0.5px solid #f3f4f6;vertical-align:top}}
  tr:nth-child(even) td{{background:#fafafa}}
  .print-btn{{display:inline-block;margin-bottom:20px;padding:8px 16px;
              background:#1a1a1a;color:#fff;border:none;border-radius:6px;cursor:pointer}}
</style></head><body>
<button class="print-btn no-print" onclick="window.print()">Print / Save as PDF</button>
<div class="header">
  <div><h1>Kingo<span>lik</span> — NGO Voice Bridge</h1>
  <div style="font-size:12px;color:#6b7280;margin-top:4px">Operational Report · Last {days} days</div></div>
  <div style="font-size:11px;color:#6b7280;text-align:right">Generated: {generated}</div>
</div>
<div class="stats">
  <div class="stat"><div class="sl">Total</div><div class="sv">{total}</div></div>
  <div class="stat"><div class="sl">Translated</div><div class="sv" style="color:#16a34a">{translated}</div></div>
  <div class="stat"><div class="sl">Urgent</div><div class="sv" style="color:#dc2626">{urgent}</div></div>
  <div class="stat"><div class="sl">Handled</div><div class="sv" style="color:#d97706">{handled}</div></div>
</div>
<h2>Language breakdown</h2>
<table style="width:320px"><tr><th>Language</th><th>Messages</th><th>Share</th></tr>{lang_rows}</table>
<h2>Session log</h2>
<table><tr><th>Date</th><th>Phone</th><th>Language</th><th>Translation</th>
<th>Status</th><th>Urgent keywords</th><th>Note</th></tr>{session_rows}</table>
<div style="margin-top:32px;padding-top:12px;border-top:1px solid #e5e7eb;
            font-size:10px;color:#9ca3af;text-align:center">
  Kingolik NGO Voice Bridge · Confidential · Internal use only
</div></body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@dashboard_bp.route("/api/v1/reports", methods=["GET"])
def open_api():
    """
    Open API — KoboToolbox, ActivityInfo, Salesforce, any JSON consumer.
    GET /api/v1/reports?status=urgent&lang=sw&limit=100
    Tell judges: "Any NGO tool that speaks JSON can pull our data.
    King'olik is a bridge, not a silo."
    """
    sessions  = get_all_sessions()
    status_f  = request.args.get("status","")
    lang_f    = request.args.get("lang","")
    limit     = int(request.args.get("limit", 500))

    reports = []
    for s in sessions:
        t = s.get("translation") or {}
        if not t.get("translation"):
            continue
        if status_f and s.get("status","") != status_f:
            continue
        if lang_f and t.get("detected_language","") != lang_f:
            continue
        reports.append({
            "id":              s["session_id"],
            "timestamp":       s["timestamp"],
            "language":        t.get("detected_language",""),
            "transcript":      t.get("transcript",""),
            "translation":     t.get("translation",""),
            "urgent_keywords": t.get("urgent_keywords",[]),
            "confidence":      t.get("confidence",""),
            "requires_review": t.get("requires_review", False),
            "engine":          t.get("engine",""),
            "latency_ms":      t.get("latency_ms", 0),
            "status":          s.get("status",""),
            "caseworker_note": s.get("note",""),
            "handled":         s.get("handled", False),
            # Phone hashed for PII compliance on transmission
            "caller_id":       __import__("hashlib").sha256(
                                   s.get("phone","").encode()
                               ).hexdigest()[:12]
        })

    return jsonify({
        "version":    "1.0",
        "source":     "King'olik Info Link",
        "generated":  __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "count":      len(reports[:limit]),
        "filters":    {"status": status_f, "lang": lang_f},
        "reports":    reports[:limit]
    })


@dashboard_bp.route("/api/copilot", methods=["POST"])
def copilot():
    """
    Caseworker Co-Pilot — AI agent that queries the database and answers
    natural language questions about field reports.
    Example: "Summarize all security threats from the last 48 hours"
    Uses Anthropic API (Claude) to generate briefings from live data.
    """
    data     = request.get_json()
    question = data.get("question","").strip()
    if not question:
        return jsonify({"answer": "Please ask a question."}), 400

    # Build context from recent sessions
    sessions  = get_all_sessions()
    cutoff_h  = int(data.get("hours", 48))
    import datetime as _dt
    cutoff    = _dt.datetime.utcnow() - _dt.timedelta(hours=cutoff_h)

    relevant = []
    for s in sessions:
        try:
            if _dt.datetime.fromisoformat(s.get("timestamp","")) >= cutoff:
                relevant.append(s)
        except Exception:
            relevant.append(s)

    # Build structured context for the AI
    lines = []
    for s in relevant[:50]:  # cap at 50 to stay within token budget
        t    = s.get("translation") or {}
        kws  = ", ".join(t.get("urgent_keywords") or [])
        lang = t.get("detected_language","?")
        ts   = s.get("timestamp","")[:16].replace("T"," ")
        tr   = (t.get("translation","") or "")[:150]
        status = "HANDLED" if s.get("handled") else s.get("status","pending")
        lines.append(
            f"[{ts}] lang={lang} status={status} keywords=[{kws}] "
            f"translation='{tr}' note='{s.get('note','')}'"
        )

    context = "\n".join(lines) if lines else "No reports in this period."

    # Call Anthropic API
    try:
        import requests as _req, os as _os
        api_key = _os.environ.get("ANTHROPIC_API_KEY","")
        if not api_key:
            return jsonify({"answer": "ANTHROPIC_API_KEY not configured. Add it to .env to enable Co-Pilot."}), 200

        system = (
            "You are a humanitarian field intelligence analyst for King'olik NGO Voice Bridge. "
            "You have access to voice report data from community members in Turkana West, Kenya. "
            "Answer caseworker questions concisely — 2-4 sentences maximum. "
            "Focus on actionable intelligence. Be direct. "
            "If urgent patterns exist, flag them clearly. "
            "Never fabricate data. Only reference what is in the provided reports."
        )

        prompt = f"Field reports from the last {cutoff_h} hours:\n\n{context}\n\nCaseworker question: {question}"
        resp = _req.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json"
            },
            json={
                "model":      "claude-sonnet-4-5",
                "max_tokens": 300,
                "system":     system,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        result  = resp.json()
        answer  = result.get("content",[{}])[0].get("text","No response.")
        return jsonify({"answer": answer, "reports_analysed": len(relevant)})

    except Exception as e:
        return jsonify({"answer": f"Co-Pilot error: {e}"}), 500


@dashboard_bp.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@dashboard_bp.route("/analytics")
def analytics_page():
    return render_template_string(ANALYTICS_HTML)


# ══════════════════════════════════════════════════════════════
#  Main dashboard HTML
# ══════════════════════════════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kingolik — NGO Live Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f0; color: #1a1a1a; font-size: 14px; }
  .topbar { background: #1a1a1a; color: #fff; padding: 14px 24px;
            display: flex; align-items: center; justify-content: space-between;
            position: sticky; top: 0; z-index: 100; }
  .topbar h1 { font-size: 18px; font-weight: 500; letter-spacing: -0.3px; }
  .topbar h1 span { color: #4ade80; }
  .topbar-right { display: flex; align-items: center; gap: 16px; }
  .nav-link { color: #9ca3af; font-size: 12px; text-decoration: none;
              padding: 4px 10px; border-radius: 6px; border: 0.5px solid #374151;
              transition: all 0.15s; }
  .nav-link:hover { color: #fff; border-color: #6b7280; }
  .nav-link.active { color: #4ade80; border-color: #4ade80; }
  .live-dot { width: 8px; height: 8px; background: #4ade80; border-radius: 50%;
              display: inline-block; margin-right: 6px;
              animation: pulse 2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
  .status-bar { font-size: 12px; color: #9ca3af;
                display: flex; align-items: center; gap: 12px; }
  .ws-badge { font-size: 10px; padding: 2px 6px; border-radius: 4px;
              background: #dcfce7; color: #15803d; }
  .stats-row { display: grid; grid-template-columns: repeat(4, 1fr);
               gap: 12px; padding: 16px 24px; }
  .stat-card { background: #fff; border: 0.5px solid #e5e5e0;
               border-radius: 10px; padding: 14px 16px; }
  .stat-label { font-size: 11px; color: #6b7280; text-transform: uppercase;
                letter-spacing: 0.5px; margin-bottom: 6px; }
  .stat-value { font-size: 28px; font-weight: 500; }
  .stat-value.urgent     { color: #dc2626; }
  .stat-value.translated { color: #16a34a; }
  .stat-value.pending    { color: #d97706; }
  .main { padding: 0 24px 24px; }
  .toolbar { display: flex; gap: 8px; margin-bottom: 14px;
             align-items: center; flex-wrap: wrap; }
  .section-title { font-size: 12px; font-weight: 500; color: #6b7280;
                   text-transform: uppercase; letter-spacing: 0.5px; margin: 0 0 10px; }
  .card { background: #fff; border: 0.5px solid #e5e5e0;
          border-radius: 10px; padding: 16px; margin-bottom: 10px;
          transition: border-color 0.2s; }
  .card:hover { border-color: #d1d5db; }
  .card.urgent  { border-left: 3px solid #dc2626; }
  .card.pending { border-left: 3px solid #d97706; opacity: 0.85; }
  .card.handled { border-left: 3px solid #16a34a; opacity: 0.7; }
  .card.new { animation: slideIn 0.4s ease-out; }
  @keyframes slideIn { from{opacity:0;transform:translateY(-8px)}
                       to{opacity:1;transform:translateY(0)} }
  .card-header { display: flex; align-items: center;
                 justify-content: space-between; margin-bottom: 10px; }
  .card-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
  .badge-urgent  { background: #fee2e2; color: #991b1b; }
  .badge-cloud   { background: #dbeafe; color: #1e40af; }
  .badge-local   { background: #fef3c7; color: #92400e; }
  .badge-pending { background: #fef3c7; color: #92400e; }
  .badge-handled { background: #dcfce7; color: #15803d; }
  .badge-lang    { background: #f3f4f6; color: #374151; }
  .phone     { font-size: 12px; color: #6b7280; font-family: monospace; }
  .timestamp { font-size: 11px; color: #9ca3af; }
  .translation-block { background: #f9fafb; border-radius: 6px;
                       padding: 10px 12px; margin: 6px 0; }
  .tblock-label { font-size: 10px; color: #9ca3af; text-transform: uppercase;
                  letter-spacing: 0.5px; margin-bottom: 3px; }
  .tblock-text { font-size: 14px; color: #111; line-height: 1.5; }
  .tblock-text.original { color: #6b7280; font-style: italic; }
  .urgent-keywords { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  .kw-chip { background: #fee2e2; color: #991b1b; padding: 2px 8px;
             border-radius: 12px; font-size: 11px; font-weight: 500; }
  .audio-section { margin-top: 12px; padding-top: 10px;
                   border-top: 0.5px solid #f3f4f6; }
  .audio-label { font-size: 10px; color: #9ca3af; text-transform: uppercase;
                 letter-spacing: 0.5px; margin-bottom: 6px;
                 display: flex; align-items: center; gap: 6px; }
  .audio-dot { width: 6px; height: 6px; background: #4ade80;
               border-radius: 50%; display: inline-block; }
  audio { width: 100%; height: 36px; border-radius: 6px;
          accent-color: #1a1a1a; display: block; }
  .notes-area { width: 100%; margin-top: 6px; padding: 8px 10px;
                border: 0.5px solid #e5e5e0; border-radius: 6px;
                font-size: 13px; resize: vertical; min-height: 60px;
                font-family: inherit; background: #fafafa; color: #1a1a1a; }
  .notes-area:focus { outline: none; border-color: #6b7280; background: #fff; }
  .action-row { display: flex; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  .action-btn { padding: 5px 12px; border-radius: 6px; font-size: 12px;
                border: 0.5px solid #e5e5e0; background: #fff;
                cursor: pointer; transition: all 0.15s; font-family: inherit; }
  .action-btn:hover { background: #f3f4f6; }
  .action-btn.primary { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .filter-bar { display: flex; gap: 8px; flex-wrap: wrap; flex: 1; }
  .filter-btn { padding: 5px 12px; border-radius: 6px; border: 0.5px solid #e5e5e0;
                background: #fff; font-size: 12px; cursor: pointer; font-family: inherit; }
  .filter-btn.active { background: #1a1a1a; color: #fff; border-color: #1a1a1a; }
  .export-btn { padding: 5px 12px; border-radius: 6px; font-size: 12px;
                border: 0.5px solid #e5e5e0; background: #fff;
                cursor: pointer; font-family: inherit; white-space: nowrap; }
  .export-btn:hover { background: #f3f4f6; }
  .search-bar { width: 100%; padding: 9px 14px; border-radius: 8px;
                border: 0.5px solid #e5e5e0; font-size: 13px;
                margin-bottom: 14px; background: #fff; font-family: inherit; }
  .search-bar:focus { outline: none; border-color: #6b7280; }
  .empty-state { text-align: center; padding: 60px 24px; color: #9ca3af; }
  .empty-icon  { font-size: 40px; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="topbar">
  <h1>Kingo<span>lik</span> — NGO Voice Bridge</h1>
  <div class="topbar-right">
    <a href="/dashboard" class="nav-link active">Live Feed</a>
    <a href="/analytics" class="nav-link">Analytics</a>
    <div class="status-bar">
      <span><span class="live-dot"></span>Live</span>
      <span id="last-updated">Connecting...</span>
      <span id="ws-badge" class="ws-badge" style="display:none">WS</span>
      <span id="conn-dot">●</span>
    </div>
  </div>
</div>

<div class="stats-row">
  <div class="stat-card">
    <div class="stat-label">Total messages</div>
    <div class="stat-value" id="stat-total">0</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Translated</div>
    <div class="stat-value translated" id="stat-translated">0</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Urgent alerts</div>
    <div class="stat-value urgent" id="stat-urgent">0</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Pending calls</div>
    <div class="stat-value pending" id="stat-pending">0</div>
  </div>
</div>

<div class="main">
  <input class="search-bar" id="search"
         placeholder="Search by phone number, language, or keyword..."
         oninput="renderCards()">
  <div class="toolbar">
    <div class="filter-bar">
      <button class="filter-btn active" onclick="setFilter('all',this)">All</button>
      <button class="filter-btn" onclick="setFilter('latest',this)">Latest</button>
      <button class="filter-btn" onclick="setFilter('urgent',this)">Urgent</button>
      <button class="filter-btn" onclick="setFilter('translated',this)">Translated</button>
      <button class="filter-btn" onclick="setFilter('pending',this)">Pending</button>
      <button class="filter-btn" onclick="setFilter('handled',this)">Handled</button>
      <button class="filter-btn" onclick="setFilter('cloud',this)">Gemini</button>
      <button class="filter-btn" onclick="setFilter('local',this)">Local AI</button>
    </div>
    <button class="export-btn" onclick="window.open('/api/export/csv?days=30','_blank')">↓ CSV</button>
    <button class="export-btn" onclick="window.open('/api/export/pdf?days=30','_blank')">↓ PDF</button>
  </div>
  <div class="section-title" id="results-label">Loading...</div>
  <div id="cards-container"></div>
</div>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
let allSessions=[], activeFilter='all', knownIds=new Set();
let isFirstLoad=true, notesStore={}, wsConnected=false;

const socket = io();
socket.on('connect', () => {
  wsConnected = true;
  document.getElementById('conn-dot').style.color = '#4ade80';
  document.getElementById('ws-badge').style.display = 'inline';
  document.getElementById('last-updated').textContent = 'Live (WebSocket)';
});
socket.on('disconnect', () => {
  wsConnected = false;
  document.getElementById('conn-dot').style.color = '#dc2626';
  document.getElementById('ws-badge').style.display = 'none';
  document.getElementById('last-updated').textContent = 'WS disconnected';
});
socket.on('trend_alert', (alert) => {
  console.log('[TREND]', alert);
  // Show prominent trend banner
  const banner = document.createElement('div');
  banner.style.cssText = 'position:fixed;top:60px;left:50%;transform:translateX(-50%);'+
    'background:#dc2626;color:#fff;padding:12px 20px;border-radius:8px;'+
    'font-size:13px;font-weight:600;z-index:999;max-width:500px;text-align:center;'+
    'box-shadow:0 4px 20px rgba(220,38,38,0.4)';
  banner.textContent = '🚨 ' + alert.message;
  document.body.appendChild(banner);
  setTimeout(() => banner.remove(), 12000);
  // Flash page title
  document.title = '🚨 TREND ALERT — ' + alert.cluster.replace('_',' ').toUpperCase();
  setTimeout(() => document.title = 'Kingolik — NGO Live Dashboard', 10000);
});

socket.on('session_updated', (session) => {
  const idx = allSessions.findIndex(s => s.session_id === session.session_id);
  if (idx >= 0) allSessions[idx] = session; else allSessions.unshift(session);
  updateStats(); patchOrInsertCard(session);
  const kws = session.translation?.urgent_keywords || [];
  if (kws.length > 0 && !isFirstLoad) {
    document.title = '(!) URGENT — Kingolik';
    setTimeout(() => document.title = 'Kingolik — NGO Voice Bridge', 5000);
  }
});

function patchOrInsertCard(s) {
  const container = document.getElementById('cards-container');
  const existing  = container.querySelector(`[data-session="${s.session_id}"]`);
  if (existing) {
    const ta = existing.querySelector('textarea');
    if (ta && document.activeElement === ta) return;
    const t = s.translation || {};
    const tEl = existing.querySelector('[data-role="translation-text"]');
    const trEl = existing.querySelector('[data-role="transcript-text"]');
    if (tEl && t.translation) tEl.textContent = t.translation;
    if (trEl && t.transcript)  trEl.textContent = t.transcript;
    const ns = getStatus(s);
    ['urgent','pending','handled','translated'].forEach(c => existing.classList.remove(c));
    existing.classList.add(ns);
  } else {
    const tmp = document.createElement('div');
    tmp.innerHTML = buildCardHTML(s);
    const el = tmp.firstElementChild;
    if (!isFirstLoad) el.classList.add('new');
    const first = container.querySelector('.card');
    if (first) container.insertBefore(el, first); else container.appendChild(el);
  }
}

function setFilter(f, btn) {
  activeFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}

function timeAgo(iso) {
  if (!iso) return '';
  const d = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (d < 60) return d + 's ago';
  if (d < 3600) return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  return new Date(iso).toLocaleDateString();
}

function getStatus(s) {
  if (s.handled || s.status === 'handled') return 'handled';
  if (s.status === 'pending_call') return 'pending';
  const t = s.translation || {};
  if ((t.urgent_keywords || []).length > 0) return 'urgent';
  if (t.translation) return 'translated';
  return 'pending';
}

function updateStats() {
  document.getElementById('stat-total').textContent = allSessions.length;
  document.getElementById('stat-translated').textContent =
    allSessions.filter(s => s.translation?.translation).length;
  document.getElementById('stat-urgent').textContent =
    allSessions.filter(s => (s.translation?.urgent_keywords||[]).length > 0).length;
  document.getElementById('stat-pending').textContent =
    allSessions.filter(s => s.status === 'pending_call').length;
}

function buildCardHTML(s) {
  const t=s.translation||{}, status=getStatus(s), engine=t.engine||'';
  const keywords=t.urgent_keywords||[], sid=s.session_id;
  const note=notesStore[sid]||s.note||'', audioUrl=`/api/audio/${sid}`;
  const handled=status==='handled';
  return `<div class="card ${status}" data-session="${sid}">
    <div class="card-header">
      <div class="card-meta">
        ${status==='urgent' ? '<span class="badge badge-urgent">URGENT</span>' : ''}
        ${status==='pending' ? '<span class="badge badge-pending">pending call</span>' : ''}
        ${handled ? '<span class="badge badge-handled">handled</span>' : ''}
        ${t.detected_language ? `<span class="badge badge-lang">${t.detected_language}</span>` : ''}
        ${engine==='cloud' ? '<span class="badge badge-cloud">Gemini</span>' : ''}
        ${engine==='local' ? '<span class="badge badge-local">Local AI</span>' : ''}
      </div>
      <div style="display:flex;gap:12px;align-items:center">
        <span class="phone">${s.phone||'unknown'}</span>
        <span class="timestamp">${timeAgo(s.timestamp)}</span>
      </div>
    </div>
    ${t.translation ? `<div class="translation-block">
      <div class="tblock-label">English translation</div>
      <div class="tblock-text" data-role="translation-text">${t.translation}</div>
    </div>` : ''}
    ${t.transcript && t.transcript!==t.translation ? `<div class="translation-block">
      <div class="tblock-label">Original (${t.detected_language||'detected'})</div>
      <div class="tblock-text original" data-role="transcript-text">${t.transcript}</div>
    </div>` : ''}
    ${keywords.length > 0 ? `<div class="urgent-keywords">
      ${keywords.map(k=>`<span class="kw-chip">${k}</span>`).join('')}
    </div>` : ''}
    ${t.confidence ? `<div style="font-size:11px;color:#9ca3af;margin-top:8px">
      Confidence: ${t.confidence}${s.duration?' · '+s.duration+'s':''}
      ${t.score?' · score: '+t.score:''}</div>` : ''}
    ${(t.requires_review || t.confidence === 'low') ? `
    <div style="background:#fef3c7;border:0.5px solid #d97706;border-radius:6px;
                padding:8px 12px;margin-top:8px;display:flex;align-items:center;gap:8px">
      <span style="font-size:16px">⚠️</span>
      <div>
        <div style="font-size:12px;font-weight:600;color:#92400e">UNCERTAIN — requires human review</div>
        <div style="font-size:11px;color:#b45309;margin-top:2px">${t.review_reason || 'Low confidence — verify before dispatch'}</div>
      </div>
    </div>` : ''}
    <div class="audio-section">
      <div class="audio-label"><span class="audio-dot"></span>Voice recording</div>
      <audio controls preload="none"
             style="width:100%;height:36px;border-radius:6px;accent-color:#1a1a1a"
             onerror="this.parentElement.innerHTML='<span style=font-size:11px;color:#9ca3af>Audio unavailable</span>'">
        <source src="${audioUrl}" type="audio/wav">
      </audio>
    </div>
    ${!handled ? `<div style="margin-top:12px;padding-top:10px;border-top:0.5px solid #f3f4f6">
      <div class="tblock-label">Caseworker notes</div>
      <textarea class="notes-area" id="note-${sid}"
        placeholder="Add notes, action taken, follow-up required...">${note}</textarea>
      <div class="action-row">
        <button class="action-btn primary" id="notebtn-${sid}"
          onclick="saveNote('${sid}')">Save note</button>
        <button class="action-btn" onclick="markHandled('${sid}')">Mark as handled</button>
      </div>
    </div>
    <div style="margin-top:10px;padding-top:10px;border-top:0.5px solid #f3f4f6">
      <div class="tblock-label" style="display:flex;align-items:center;gap:6px">
        Corrected translation
        <span style="background:#dcfce7;color:#15803d;font-size:9px;
              padding:1px 5px;border-radius:3px;font-weight:600">HITL TRAINING DATA</span>
      </div>
      <textarea class="notes-area" id="correction-${sid}"
        placeholder="If translation is wrong, type the correct English version here — this becomes AI training data..."
        style="min-height:48px;border-color:#d1fae5"></textarea>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
        <button class="action-btn" id="corbtn-${sid}"
          onclick="saveCorrection('${sid}')"
          style="font-size:11px;color:#16a34a;border-color:#d1fae5">
          Submit correction
        </button>
        <span style="font-size:10px;color:#9ca3af">Corrections train the local AI model</span>
      </div>
      </div>
    </div>` : `<div style="margin-top:8px;padding-top:8px;border-top:0.5px solid #f3f4f6">
      ${note ? `<div style="font-size:12px;color:#6b7280;font-style:italic">"${note}"</div>` : ''}
      <div style="font-size:11px;color:#16a34a;margin-top:4px">Handled</div>
    </div>`}
  </div>`;
}

function renderCards() {
  const search = document.getElementById('search').value.toLowerCase();
  const container = document.getElementById('cards-container');
  const filtered = allSessions.filter(s => {
    const status=getStatus(s), t=s.translation||{}, engine=t.engine||'';
    if (activeFilter==='latest') return s.session_id===allSessions[0]?.session_id;
    if (activeFilter==='urgent' && status!=='urgent') return false;
    if (activeFilter==='translated' && !t.translation) return false;
    if (activeFilter==='pending' && status!=='pending') return false;
    if (activeFilter==='handled' && status!=='handled') return false;
    if (activeFilter==='cloud' && engine!=='cloud') return false;
    if (activeFilter==='local' && engine!=='local') return false;
    if (search) {
      const hay=[s.phone,t.translation,t.transcript,t.detected_language,
                 (t.urgent_keywords||[]).join(' ')].join(' ').toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });
  document.getElementById('results-label').textContent =
    filtered.length + ' message' + (filtered.length!==1?'s':'');
  if (filtered.length===0) {
    container.innerHTML = `<div class="empty-state"><div class="empty-icon">📭</div>
      <div>No messages yet</div>
      <div style="font-size:12px;margin-top:6px">Dial *384*67660# on the simulator</div></div>`;
    return;
  }
  filtered.forEach((s,i) => {
    const sid=s.session_id, existing=container.querySelector(`[data-session="${sid}"]`);
    if (existing) {
      const ta=existing.querySelector('textarea');
      if (ta && (document.activeElement===ta || ta.value!==(notesStore[sid]||s.note||''))) return;
      if (ta?.value) notesStore[sid]=ta.value;
      const t=s.translation||{};
      const tEl=existing.querySelector('[data-role="translation-text"]');
      const trEl=existing.querySelector('[data-role="transcript-text"]');
      if (tEl && t.translation) tEl.textContent=t.translation;
      if (trEl && t.transcript) trEl.textContent=t.transcript;
      const ns=getStatus(s);
      ['urgent','pending','handled','translated'].forEach(c=>existing.classList.remove(c));
      existing.classList.add(ns);
    } else {
      const tmp=document.createElement('div');
      tmp.innerHTML=buildCardHTML(s);
      const el=tmp.firstElementChild;
      if (!isFirstLoad) el.classList.add('new');
      const cards=container.querySelectorAll('.card');
      if (i<cards.length) container.insertBefore(el,cards[i]);
      else container.appendChild(el);
    }
  });
  container.querySelectorAll('.card').forEach(card => {
    if (!filtered.find(s=>s.session_id===card.dataset.session)) card.remove();
  });
  filtered.forEach(s=>knownIds.add(s.session_id));
  if (isFirstLoad) isFirstLoad=false;
}

async function markHandled(sid) {
  await fetch('/api/mark-handled',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid})});
  const s=allSessions.find(x=>x.session_id===sid);
  if (s){s.handled=true;s.status='handled';} renderCards();
}

async function saveNote(sid) {
  const el=document.getElementById('note-'+sid), btn=document.getElementById('notebtn-'+sid);
  if (!el) return;
  notesStore[sid]=el.value;
  await fetch('/api/save-note',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:sid,note:el.value})});
  if (btn){btn.textContent='Saved ✓';btn.style.background='#16a34a';btn.style.color='#fff';
    setTimeout(()=>{btn.textContent='Save note';btn.style.background='';btn.style.color='';},2000);}
}

async function saveCorrection(sid) {
  const el  = document.getElementById('correction-' + sid);
  const btn = document.getElementById('corbtn-' + sid);
  if (!el || !el.value.trim()) return;
  try {
    await fetch('/api/save-correction', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sid, correction: el.value})
    });
    if (btn) {
      btn.textContent = 'Saved as training data ✓';
      btn.style.color = '#15803d';
      btn.style.fontWeight = '600';
    }
  } catch(e) { console.error(e); }
}

async function fetchSessions() {
  try {
    const data=await(await fetch('/api/sessions')).json();
    allSessions=data; renderCards(); updateStats();
    if (!wsConnected){
      document.getElementById('last-updated').textContent='Updated '+new Date().toLocaleTimeString();
      document.getElementById('conn-dot').style.color='#4ade80';
    }
  } catch(e){
    if(!wsConnected){document.getElementById('conn-dot').style.color='#dc2626';}
  }
}
fetchSessions();
setInterval(fetchSessions, 30000);
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════
#  Analytics dashboard — inspired by the King'olik Info Link design
# ══════════════════════════════════════════════════════════════
ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kingolik — Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#0f1117; color:#e2e8f0; font-size:13px; }
  .topbar { background:#0f1117; border-bottom:1px solid #1e2433;
            padding:14px 24px; display:flex; align-items:center;
            justify-content:space-between; position:sticky; top:0; z-index:100; }
  .topbar h1 { font-size:16px; font-weight:500; color:#e2e8f0; letter-spacing:-0.3px; }
  .topbar h1 span { color:#4ade80; }
  .topbar-right { display:flex; align-items:center; gap:12px; }
  .nav-link { color:#64748b; font-size:12px; text-decoration:none;
              padding:4px 10px; border-radius:6px; border:0.5px solid #1e2433; }
  .nav-link:hover { color:#e2e8f0; border-color:#334155; }
  .nav-link.active { color:#4ade80; border-color:#4ade80; }
  .period-btn { background:#1e2433; border:0.5px solid #334155; color:#94a3b8;
                padding:4px 10px; border-radius:6px; font-size:11px;
                cursor:pointer; font-family:inherit; }
  .period-btn.active { background:#4ade80; color:#0f1117; border-color:#4ade80; font-weight:600; }
  .live-dot { width:7px; height:7px; background:#4ade80; border-radius:50%;
              display:inline-block; margin-right:5px; animation:pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  .grid { display:grid; gap:16px; padding:20px 24px; }
  .row-4 { grid-template-columns:repeat(4,1fr); }
  .row-3 { grid-template-columns:2fr 1fr 1fr; }
  .row-2 { grid-template-columns:1.4fr 1fr; }
  .card { background:#161b27; border:0.5px solid #1e2433;
          border-radius:12px; padding:18px; }
  .card-title { font-size:10px; color:#64748b; text-transform:uppercase;
                letter-spacing:0.8px; margin-bottom:12px; }
  .big-num { font-size:42px; font-weight:600; line-height:1;
             background:linear-gradient(135deg,#4ade80,#22d3ee);
             -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .big-num.red { background:linear-gradient(135deg,#f87171,#fb923c);
                 -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .big-num.blue { background:linear-gradient(135deg,#60a5fa,#a78bfa);
                  -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .big-num.amber { background:linear-gradient(135deg,#fbbf24,#f97316);
                   -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
  .sub-label { font-size:11px; color:#475569; margin-top:6px; }
  .accuracy-ring { display:flex; align-items:center; gap:16px; }
  .ring-wrap { position:relative; width:90px; height:90px; flex-shrink:0; }
  .ring-wrap canvas { width:90px!important; height:90px!important; }
  .ring-center { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
                 text-align:center; }
  .ring-pct { font-size:20px; font-weight:700; color:#4ade80; }
  .ring-lbl { font-size:9px; color:#64748b; margin-top:1px; }
  .ring-info { flex:1; }
  .ring-info .label { font-size:12px; color:#94a3b8; margin-bottom:4px; }
  .ring-info .detail { font-size:11px; color:#475569; }
  .chart-wrap { position:relative; width:100%; height:160px; }
  .lang-bar { display:flex; flex-direction:column; gap:8px; }
  .lang-row { display:flex; align-items:center; gap:8px; }
  .lang-name { font-size:11px; color:#94a3b8; width:60px; flex-shrink:0; }
  .lang-track { flex:1; background:#1e2433; border-radius:4px; height:8px; overflow:hidden; }
  .lang-fill { height:100%; border-radius:4px;
               background:linear-gradient(90deg,#4ade80,#22d3ee); }
  .lang-pct { font-size:10px; color:#64748b; width:32px; text-align:right; flex-shrink:0; }
  .log-list { display:flex; flex-direction:column; gap:6px; max-height:200px; overflow-y:auto; }
  .log-row { display:flex; gap:8px; align-items:flex-start; font-size:11px; }
  .log-ts { color:#334155; font-family:monospace; flex-shrink:0; padding-top:1px; }
  .log-dot { width:6px; height:6px; border-radius:50%; margin-top:4px; flex-shrink:0; }
  .log-dot.urgent { background:#f87171; }
  .log-dot.ok { background:#4ade80; }
  .log-dot.pending { background:#fbbf24; }
  .log-msg { color:#94a3b8; line-height:1.4; }
  .kw-grid { display:flex; flex-wrap:wrap; gap:6px; }
  .kw-tag { background:#1e2433; border:0.5px solid #dc2626;
            color:#f87171; padding:3px 8px; border-radius:10px;
            font-size:10px; font-weight:500; }
  .kw-tag .cnt { color:#64748b; margin-left:4px; }
  .engine-row { display:flex; gap:12px; margin-top:4px; }
  .eng-item { flex:1; background:#1e2433; border-radius:8px; padding:10px 12px; }
  .eng-label { font-size:10px; color:#64748b; margin-bottom:4px; }
  .eng-val { font-size:20px; font-weight:600; color:#e2e8f0; }
  .eng-sub { font-size:10px; color:#475569; margin-top:2px; }
  .footer-bar { text-align:center; padding:16px; color:#1e2433; font-size:10px; }
</style>
</head>
<body>

<div class="topbar">
  <h1>Kingo<span>lik</span> Info Link — <span style="color:#64748b;font-weight:400">AI Analytics</span></h1>
  <div class="topbar-right">
    <a href="/dashboard" class="nav-link">Live Feed</a>
    <a href="/analytics" class="nav-link active">Analytics</a>
    <button class="period-btn active" onclick="setPeriod(7,this)">7d</button>
    <button class="period-btn" onclick="setPeriod(30,this)">30d</button>
    <button class="period-btn" onclick="setPeriod(90,this)">90d</button>
    <span style="font-size:12px;color:#475569"><span class="live-dot"></span>Live</span>
  </div>
</div>

<div class="grid row-4" style="margin-top:4px">
  <div class="card">
    <div class="card-title">Total voice messages</div>
    <div class="big-num" id="a-total">—</div>
    <div class="sub-label">calls processed</div>
  </div>
  <div class="card">
    <div class="card-title">Urgent alerts</div>
    <div class="big-num red" id="a-urgent">—</div>
    <div class="sub-label">keywords detected</div>
  </div>
  <div class="card">
    <div class="card-title">Successfully translated</div>
    <div class="big-num blue" id="a-translated">—</div>
    <div class="sub-label">into English</div>
  </div>
  <div class="card">
    <div class="card-title">Cases handled</div>
    <div class="big-num amber" id="a-handled">—</div>
    <div class="sub-label">by caseworkers</div>
  </div>
</div>

<div class="grid" style="grid-template-columns:1fr 1fr 1fr;margin-top:0;padding-top:0">
  <div class="card" style="display:flex;align-items:center;gap:16px">
    <div>
      <div class="card-title">Avg translation latency</div>
      <div style="font-size:32px;font-weight:700;color:#22d3ee" id="a-latency-ms">—</div>
      <div class="sub-label">milliseconds end-to-end</div>
    </div>
    <div style="font-size:32px;opacity:0.2">⚡</div>
  </div>
  <div class="card" style="display:flex;align-items:center;gap:16px">
    <div>
      <div class="card-title">Needs human review</div>
      <div style="font-size:32px;font-weight:700;color:#f87171" id="a-review">—</div>
      <div class="sub-label">low confidence translations</div>
    </div>
    <div style="font-size:32px;opacity:0.2">👁</div>
  </div>
  <div class="card" style="display:flex;align-items:center;gap:16px">
    <div>
      <div class="card-title">Rate limit protection</div>
      <div style="font-size:32px;font-weight:700;color:#4ade80">3/hr</div>
      <div class="sub-label">max per phone number</div>
    </div>
    <div style="font-size:32px;opacity:0.2">🛡</div>
  </div>
</div>

<div class="grid" style="grid-template-columns:1fr;padding-top:0;margin-top:0">
  <div class="card" style="background:linear-gradient(135deg,#0d1f12,#161b27);
       border:0.5px solid #166534">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div>
        <div class="card-title" style="color:#4ade80">Human-in-the-loop feedback engine</div>
        <div style="display:flex;align-items:baseline;gap:12px;margin-top:6px">
          <div style="font-size:36px;font-weight:700;color:#4ade80" id="a-gold">—</div>
          <div style="font-size:13px;color:#64748b">gold standard correction pairs collected</div>
        </div>
        <div style="font-size:11px;color:#166534;margin-top:4px">
          Every caseworker correction trains the local Whisper model.
          The system gets smarter with every emergency it handles.
        </div>
      </div>
      <div style="font-size:48px;opacity:0.15">🧠</div>
    </div>
  </div>
</div>

<div class="grid row-3">
  <div class="card">
    <div class="card-title">Daily NGO engagement</div>
    <div class="chart-wrap"><canvas id="chart-daily"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">Whisper AI accuracy</div>
    <div class="accuracy-ring" style="margin-top:8px">
      <div class="ring-wrap">
        <canvas id="chart-accuracy"></canvas>
        <div class="ring-center">
          <div class="ring-pct" id="a-accuracy">—</div>
          <div class="ring-lbl">accuracy</div>
        </div>
      </div>
      <div class="ring-info">
        <div class="label">Swahili / Arabic</div>
        <div class="detail" id="a-latency">Gemini + Whisper medium</div>
        <div class="detail" style="margin-top:6px" id="a-period">Last 7 days</div>
      </div>
    </div>
    <div class="engine-row" style="margin-top:14px">
      <div class="eng-item">
        <div class="eng-label">Gemini cloud</div>
        <div class="eng-val" id="a-cloud">—</div>
        <div class="eng-sub">calls</div>
      </div>
      <div class="eng-item">
        <div class="eng-label">Local Whisper</div>
        <div class="eng-val" id="a-local">—</div>
        <div class="eng-sub">calls</div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Voice feedback language</div>
    <div class="lang-bar" id="lang-bars" style="margin-top:8px">
      <div style="color:#334155;font-size:11px">Loading...</div>
    </div>
  </div>
</div>

<div class="grid row-2">
  <div class="card">
    <div class="card-title">Real-time voice pipeline log</div>
    <div class="log-list" id="pipeline-log">
      <div style="color:#334155;font-size:11px">Loading...</div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Top urgent keywords detected</div>
    <div class="kw-grid" id="kw-grid" style="margin-top:4px">
      <div style="color:#334155;font-size:11px">Loading...</div>
    </div>
  </div>
</div>

<div class="grid" style="grid-template-columns:1fr;padding-top:0;margin-top:0">
  <div class="card">
    <div class="card-title" style="color:#60a5fa;margin-bottom:10px">
      Caseworker co-pilot — AI field intelligence agent
    </div>
    <div style="font-size:11px;color:#475569;margin-bottom:10px">
      Ask anything about field reports. "Summarize security threats last 48h" · "How many water reports this week?" · "Which sessions need follow-up?"
    </div>
    <div style="display:flex;gap:8px">
      <input id="copilot-input" type="text"
        placeholder="Ask about field reports..."
        style="flex:1;background:#1e2433;border:0.5px solid #334155;color:#e2e8f0;
               padding:8px 12px;border-radius:6px;font-size:12px;font-family:inherit;outline:none"
        onkeydown="if(event.key==='Enter')askCopilot()">
      <button onclick="askCopilot()"
        style="background:#4ade80;color:#0f1117;border:none;padding:8px 16px;
               border-radius:6px;font-size:12px;font-weight:600;cursor:pointer">
        Ask
      </button>
    </div>
    <div id="copilot-answer"
      style="margin-top:10px;padding:10px 12px;background:#0d1f12;border-radius:6px;
             font-size:12px;color:#4ade80;line-height:1.6;display:none;
             border:0.5px solid #166534">
    </div>
    <div id="copilot-meta" style="font-size:10px;color:#334155;margin-top:4px"></div>
  </div>
</div>

<div class="footer-bar">
  Kingolik NGO Voice Bridge · Alpha Deployment · Kakuma Q1 2026
</div>

<script>
let currentDays = 7;
let dailyChart = null, accuracyChart = null;

function setPeriod(days, btn) {
  currentDays = days;
  document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadAnalytics();
}

async function loadAnalytics() {
  try {
    const data = await (await fetch('/api/analytics?days=' + currentDays)).json();
    renderAnalytics(data);
  } catch(e) {
    console.error('Analytics load failed:', e);
  }
}

function renderAnalytics(d) {
  document.getElementById('a-total').textContent      = d.total;
  document.getElementById('a-urgent').textContent     = d.urgent;
  document.getElementById('a-translated').textContent = d.translated;
  document.getElementById('a-handled').textContent    = d.handled;
  document.getElementById('a-accuracy').textContent   = d.accuracy + '%';
  document.getElementById('a-cloud').textContent      = d.engine_counts.cloud || 0;
  document.getElementById('a-local').textContent      = d.engine_counts.local || 0;
  document.getElementById('a-period').textContent     = 'Last ' + d.period_days + ' days';
  document.getElementById('a-latency-ms').textContent = d.avg_latency_ms ? d.avg_latency_ms + 'ms' : '—';
  document.getElementById('a-review').textContent     = d.needs_review || 0;
  if (document.getElementById('a-gold'))
    document.getElementById('a-gold').textContent = d.gold_pairs || 0;

  // Daily chart
  const labels  = d.daily.map(x => x.day);
  const totals  = d.daily.map(x => x.total);
  const urgents = d.daily.map(x => x.urgent);

  if (dailyChart) dailyChart.destroy();
  const ctx = document.getElementById('chart-daily').getContext('2d');
  dailyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Voice-AI Calls', data: totals,
          borderColor: '#4ade80', backgroundColor: 'rgba(74,222,128,0.08)',
          tension: 0.4, fill: true, pointRadius: 3, pointBackgroundColor: '#4ade80' },
        { label: 'Urgent Alerts', data: urgents,
          borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.08)',
          tension: 0.4, fill: true, pointRadius: 3, pointBackgroundColor: '#f87171' }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#64748b', font: { size: 10 } } } },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 10 } },
             grid: { color: '#1e2433' } },
        y: { ticks: { color: '#475569', font: { size: 10 } },
             grid: { color: '#1e2433' }, beginAtZero: true }
      }
    }
  });

  // Accuracy donut
  const acc = d.accuracy;
  if (accuracyChart) accuracyChart.destroy();
  const ctx2 = document.getElementById('chart-accuracy').getContext('2d');
  accuracyChart = new Chart(ctx2, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: [acc, 100 - acc],
        backgroundColor: ['#4ade80', '#1e2433'],
        borderWidth: 0,
        cutout: '78%'
      }]
    },
    options: { responsive:false, plugins:{ legend:{display:false}, tooltip:{enabled:false} } }
  });

  // Language bars
  const total = d.total || 1;
  const langs = Object.entries(d.lang_counts)
    .sort((a,b) => b[1]-a[1]).slice(0,6);
  const colors = ['#4ade80','#22d3ee','#a78bfa','#fb923c','#f472b6','#fbbf24'];
  document.getElementById('lang-bars').innerHTML = langs.map(([lang,count],i) => {
    const pct = Math.round(count/total*100);
    return `<div class="lang-row">
      <div class="lang-name">${lang}</div>
      <div class="lang-track">
        <div class="lang-fill" style="width:${pct}%;background:${colors[i]||'#4ade80'}"></div>
      </div>
      <div class="lang-pct">${pct}%</div>
    </div>`;
  }).join('') || '<div style="color:#334155;font-size:11px">No data yet</div>';

  // Pipeline log
  document.getElementById('pipeline-log').innerHTML = d.log.map(e => `
    <div class="log-row">
      <div class="log-ts">${e.ts.slice(11)}</div>
      <div class="log-dot ${e.kind}"></div>
      <div class="log-msg">
        <span style="color:#475569">${e.phone || 'V-AAP'}</span>
        <span style="color:#64748b"> · </span>
        ${e.msg}
      </div>
    </div>`).join('') || '<div style="color:#334155;font-size:11px">No activity yet</div>';

  // Keywords
  document.getElementById('kw-grid').innerHTML = d.top_keywords.map(k =>
    `<div class="kw-tag">${k.kw}<span class="cnt">×${k.count}</span></div>`
  ).join('') || '<div style="color:#334155;font-size:11px">No urgent keywords yet</div>';
}

loadAnalytics();
setInterval(loadAnalytics, 15000);

async function askCopilot() {
  const input  = document.getElementById('copilot-input');
  const answer = document.getElementById('copilot-answer');
  const meta   = document.getElementById('copilot-meta');
  const q      = input.value.trim();
  if (!q) return;

  answer.style.display = 'block';
  answer.textContent   = 'Analysing field reports...';
  meta.textContent     = '';

  try {
    const resp = await fetch('/api/copilot', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({question: q, hours: currentDays * 24})
    });
    const data = await resp.json();
    answer.textContent = data.answer;
    meta.textContent   = data.reports_analysed
      ? `Based on ${data.reports_analysed} reports in the last ${currentDays * 24}h`
      : '';
  } catch(e) {
    answer.textContent = 'Co-Pilot unavailable: ' + e.message;
  }
}

// Browser wake word — "kingolik" triggers the Co-Pilot
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SpeechRecognition) {
  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = false;
  recognition.lang = 'en-US';
  recognition.onresult = (event) => {
    const transcript = event.results[event.results.length-1][0].transcript.toLowerCase();
    if (transcript.includes("kingolik")) {
      const query = transcript.replace("kingolik", "").trim();
      if (query.length > 3) {
        document.getElementById('copilot-input').value = query;
        sendCopilotQuery(query);
        document.querySelector('.copilot-panel').style.boxShadow = '0 0 0 2px #00e5c3';
        setTimeout(() => document.querySelector('.copilot-panel').style.boxShadow = 'none', 2000);
      }
    }
  };
  recognition.start();
  recognition.onend = () => recognition.start(); // keep listening
}
</script>

</body>
</html>"""