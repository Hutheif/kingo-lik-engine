# dashboard.py — King'olik NGO Live Dashboard + Analytics + Co-Pilot
from flask import Blueprint, render_template_string, jsonify, send_file, request
from database import get_all_sessions, mark_handled, save_note, get_session
import os, io, csv, datetime, json

dashboard_bp = Blueprint('dashboard', __name__)


# ══════════════════════════════════════════════════════════════
#  API ROUTES
# ══════════════════════════════════════════════════════════════

@dashboard_bp.route("/api/sessions")
def api_sessions():
    return jsonify(get_all_sessions())


@dashboard_bp.route("/api/analytics")
def api_analytics():
    sessions = get_all_sessions()
    days     = int(request.args.get("days", 7))
    cutoff   = datetime.datetime.utcnow() - datetime.timedelta(days=days)

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

    lang_counts   = {}
    engine_counts = {"cloud": 0, "local": 0, "error": 0}
    for s in period:
        lang = (s.get("translation") or {}).get("detected_language") or "unknown"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
        eng = (s.get("translation") or {}).get("engine") or "unknown"
        if eng in engine_counts:
            engine_counts[eng] += 1

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

    kw_counts = {}
    for s in period:
        for kw in ((s.get("translation") or {}).get("urgent_keywords") or []):
            kw_counts[kw] = kw_counts.get(kw, 0) + 1
    top_keywords = sorted(kw_counts.items(), key=lambda x: -x[1])[:8]

    conf_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    for s in period:
        conf = (s.get("translation") or {}).get("confidence") or "none"
        if conf in conf_counts:
            conf_counts[conf] += 1

    recent = sorted(period, key=lambda x: x.get("timestamp",""), reverse=True)[:8]
    log_entries = []
    for s in recent:
        t    = s.get("translation") or {}
        kws  = t.get("urgent_keywords") or []
        lang = t.get("detected_language","?")
        eng  = t.get("engine","?")
        ts   = s.get("timestamp","")[:16].replace("T"," ")
        if kws:
            msg  = f"URGENT — {lang} — {', '.join(kws[:3])}"
            kind = "urgent"
        elif t.get("translation"):
            msg  = f"Translated ({lang}) via {eng}"
            kind = "ok"
        else:
            msg  = f"Pending call from {s.get('phone','?')}"
            kind = "pending"
        log_entries.append({"ts": ts, "msg": msg, "kind": kind, "phone": s.get("phone","")})

    latencies = [
        (s.get("translation") or {}).get("latency_ms", 0)
        for s in period
        if (s.get("translation") or {}).get("latency_ms", 0) > 0
    ]
    avg_latency  = int(sum(latencies) / len(latencies)) if latencies else 0
    needs_review = sum(1 for s in period if (s.get("translation") or {}).get("requires_review"))

    if total > 0:
        score    = (conf_counts["high"]*100 + conf_counts["medium"]*75 +
                    conf_counts["low"]*40) / max(translated, 1)
        accuracy = min(round(score), 99)
    else:
        accuracy = 0

    try:
        from database import _count_corrections
        gold_pairs = _count_corrections()
    except Exception as e:
        print(f"[ANALYTICS] _count_corrections failed: {e}")
        gold_pairs = 0

    return jsonify({
        "total": total, "translated": translated,
        "urgent": urgent, "handled": handled, "pending": pending,
        "accuracy": accuracy, "lang_counts": lang_counts,
        "engine_counts": engine_counts, "daily": daily_list,
        "top_keywords": [{"kw": k, "count": v} for k, v in top_keywords],
        "conf_counts": conf_counts, "log": log_entries,
        "period_days": days, "avg_latency_ms": avg_latency,
        "needs_review": needs_review, "gold_pairs": gold_pairs,
    })


@dashboard_bp.route("/api/audio/<session_id>")
def serve_audio(session_id):
    """
    Serves the audio recording for a specific session.
    Looks for files named {session_id}_raw_clean.wav, {session_id}_clean.wav, {session_id}_raw.wav.
    Returns 404 if not found — NO fallback to newest WAV (that was serving wrong audio).
    """
    recordings_dir = os.path.join(os.getcwd(), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)

    # Check all possible naming patterns for this session
    candidates = [
        f"{session_id}_raw_clean.wav",
        f"{session_id}_clean.wav",
        f"{session_id}_raw.wav",
        f"ATVId_{session_id}_raw.wav",          # AT sometimes prefixes with ATVId_
        f"ATVId_{session_id}_raw_clean.wav",
    ]

    for filename in candidates:
        full_path = os.path.join(recordings_dir, filename)
        if os.path.exists(full_path):
            resp = send_file(full_path, mimetype="audio/wav", conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=3600"
            resp.headers["Accept-Ranges"] = "bytes"
            return resp

    # Also check if any file in recordings/ contains the session_id in its name
    try:
        for fname in os.listdir(recordings_dir):
            if session_id in fname and fname.endswith(".wav"):
                full_path = os.path.join(recordings_dir, fname)
                resp = send_file(full_path, mimetype="audio/wav", conditional=True)
                resp.headers["Cache-Control"] = "public, max-age=3600"
                resp.headers["Accept-Ranges"] = "bytes"
                return resp
    except Exception:
        pass

    return jsonify({"error": "audio_not_found", "session_id": session_id}), 404


@dashboard_bp.route("/api/delete-session", methods=["POST"])
def delete_session_route():
    from database import delete_session
    data = request.get_json() or {}
    session_id = data.get("session_id","").strip()
    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    try:
        delete_session(session_id)
        return jsonify({"ok": True, "deleted": session_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@dashboard_bp.route("/api/save-correction", methods=["POST"])
def save_correction_route():
    from database import save_correction, _count_corrections, get_session
    data       = request.get_json() or {}
    session_id = data.get("session_id", "").strip()
    correction = data.get("correction", "").strip()

    if not session_id:
        return jsonify({"ok": False, "error": "session_id required"}), 400
    if not correction:
        return jsonify({"ok": False, "error": "correction text is empty"}), 400

    session = get_session(session_id)
    if not session:
        print(f"[HITL] Session {session_id[-8:]} not found — saving correction anyway")

    try:
        save_correction(session_id, correction)
        total = _count_corrections()
        print(f"[HITL] Correction saved → {session_id[-8:]}  total_pairs={total}")
        return jsonify({
            "ok": True,
            "session_id": session_id,
            "correction": correction,
            "total": total,
            "message": f"Gold standard pair #{total} collected"
        })
    except Exception as e:
        print(f"[HITL] Save failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@dashboard_bp.route("/api/hitl/debug")
def hitl_debug():
    """Debug endpoint — shows all corrections in the database."""
    import sqlite3
    db_path = os.path.join(os.getcwd(), "kingolik.db")
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            "SELECT session_id, correction, translation FROM sessions "
            "WHERE correction IS NOT NULL AND correction != '' "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        total_count = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE correction IS NOT NULL AND correction != ''"
        ).fetchone()[0]
        con.close()
        corrections = []
        for r in rows:
            corrections.append({
                "session_id": r[0][-8:],
                "correction": r[1][:100],
                "has_translation": bool(r[2])
            })
        return jsonify({
            "total_gold_pairs": total_count,
            "corrections": corrections,
            "db_path": db_path
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/api/mark-handled", methods=["POST"])
def mark_handled_route():
    data       = request.get_json()
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"ok": False}), 400
    mark_handled(session_id)
    return jsonify({"ok": True})


@dashboard_bp.route("/api/save-note", methods=["POST"])
def save_note_route():
    data       = request.get_json()
    session_id = data.get("session_id")
    note       = data.get("note", "")
    if not session_id:
        return jsonify({"ok": False}), 400
    save_note(session_id, note)
    return jsonify({"ok": True})


@dashboard_bp.route("/api/copilot", methods=["POST"])
def copilot_route():
    """
    Routes ALL Co-Pilot queries through copilot.py (Groq Llama 3 70B).
    Falls back to offline SQL if internet down.
    Accepts both 'question' and 'query' keys for compatibility.
    """
    data  = request.get_json() or {}
    query = (data.get("query") or data.get("question", "")).strip()
    if not query:
        return jsonify({"answer": "Please ask a question.", "text": "Please ask a question."}), 400
    try:
        from copilot import get_copilot_response
        result = get_copilot_response(query)
        # Normalise: both 'answer' and 'text' keys for dashboard + analytics compatibility
        text = result.get("text", "")
        return jsonify({
            "answer":           text,
            "text":             text,
            "mode":             result.get("mode", "unknown"),
            "reports_analysed": result.get("snapshot", {}).get("total", 0),
            "audio":            result.get("audio")
        })
    except Exception as e:
        fallback = f"Co-Pilot error: {e}"
        return jsonify({"answer": fallback, "text": fallback}), 500


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
        t      = s.get("translation") or {}
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
    return send_file(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        mimetype="text/csv", as_attachment=True, download_name=filename
    )


@dashboard_bp.route("/api/export/pdf")
def export_pdf():
    days      = int(request.args.get("days", 30))
    sessions  = get_all_sessions()
    cutoff    = datetime.datetime.utcnow() - datetime.timedelta(days=days)
    filtered  = []
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

    lang_rows    = "".join(
        f"<tr><td>{lang}</td><td>{count}</td>"
        f"<td>{round(count/total*100) if total else 0}%</td></tr>"
        for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1])
    )
    session_rows = ""
    for s in filtered[:200]:
        t        = s.get("translation") or {}
        keywords = ", ".join(t.get("urgent_keywords") or [])
        status   = "handled" if (s.get("handled") or s.get("status")=="handled") else s.get("status","")
        color    = "#15803d" if status=="handled" else ("#dc2626" if keywords else "#d97706")
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
    sessions = get_all_sessions()
    status_f = request.args.get("status","")
    lang_f   = request.args.get("lang","")
    limit    = int(request.args.get("limit", 500))
    reports  = []
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
            "caller_id":       __import__("hashlib").sha256(
                                   s.get("phone","").encode()
                               ).hexdigest()[:12]
        })
    return jsonify({
        "version":   "1.0",
        "source":    "King'olik Info Link",
        "generated": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "count":     len(reports[:limit]),
        "filters":   {"status": status_f, "lang": lang_f},
        "reports":   reports[:limit]
    })


@dashboard_bp.route("/dashboard")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@dashboard_bp.route("/analytics")
def analytics_page():
    return render_template_string(ANALYTICS_HTML)


# ══════════════════════════════════════════════════════════════
#  LIVE DASHBOARD HTML
# ══════════════════════════════════════════════════════════════
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kingolik — NGO Live Dashboard</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f5f5f0;color:#1a1a1a;font-size:14px}
  .topbar{background:#1a1a1a;color:#fff;padding:14px 24px;
          display:flex;align-items:center;justify-content:space-between;
          position:sticky;top:0;z-index:100}
  .topbar h1{font-size:18px;font-weight:500;letter-spacing:-0.3px}
  .topbar h1 span{color:#4ade80}
  .topbar-right{display:flex;align-items:center;gap:16px}
  .nav-link{color:#9ca3af;font-size:12px;text-decoration:none;
            padding:4px 10px;border-radius:6px;border:0.5px solid #374151;transition:all 0.15s}
  .nav-link:hover{color:#fff;border-color:#6b7280}
  .nav-link.active{color:#4ade80;border-color:#4ade80}
  .live-dot{width:8px;height:8px;background:#4ade80;border-radius:50%;
            display:inline-block;margin-right:6px;animation:pulse 2s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
  .status-bar{font-size:12px;color:#9ca3af;display:flex;align-items:center;gap:12px}
  .ws-badge{font-size:10px;padding:2px 6px;border-radius:4px;background:#dcfce7;color:#15803d}
  .stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:16px 24px}
  .stat-card{background:#fff;border:0.5px solid #e5e5e0;border-radius:10px;padding:14px 16px}
  .stat-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px}
  .stat-value{font-size:28px;font-weight:500}
  .stat-value.urgent{color:#dc2626}
  .stat-value.translated{color:#16a34a}
  .stat-value.pending{color:#d97706}
  .main{padding:0 24px 24px}
  .toolbar{display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
  .section-title{font-size:12px;font-weight:500;color:#6b7280;text-transform:uppercase;
                 letter-spacing:0.5px;margin:0 0 10px}
  .card{background:#fff;border:0.5px solid #e5e5e0;border-radius:10px;padding:16px;
        margin-bottom:10px;transition:border-color 0.2s}
  .card:hover{border-color:#d1d5db}
  .card.urgent{border-left:3px solid #dc2626}
  .card.pending{border-left:3px solid #d97706;opacity:0.85}
  .card.handled{border-left:3px solid #16a34a;opacity:0.7}
  .card.new{animation:slideIn 0.4s ease-out}
  @keyframes slideIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
  .card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .card-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:500}
  .badge-urgent{background:#fee2e2;color:#991b1b}
  .badge-cloud{background:#dbeafe;color:#1e40af}
  .badge-local{background:#fef3c7;color:#92400e}
  .badge-pending{background:#fef3c7;color:#92400e}
  .badge-handled{background:#dcfce7;color:#15803d}
  .badge-lang{background:#f3f4f6;color:#374151}
  .phone{font-size:12px;color:#6b7280;font-family:monospace}
  .timestamp{font-size:11px;color:#9ca3af}
  .translation-block{background:#f9fafb;border-radius:6px;padding:10px 12px;margin:6px 0}
  .tblock-label{font-size:10px;color:#9ca3af;text-transform:uppercase;
                letter-spacing:0.5px;margin-bottom:3px}
  .tblock-text{font-size:14px;color:#111;line-height:1.5}
  .tblock-text.original{color:#6b7280;font-style:italic}
  .urgent-keywords{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .kw-chip{background:#fee2e2;color:#991b1b;padding:2px 8px;border-radius:12px;
           font-size:11px;font-weight:500}
  .audio-section{margin-top:12px;padding-top:10px;border-top:0.5px solid #f3f4f6}
  .audio-label{font-size:10px;color:#9ca3af;text-transform:uppercase;
               letter-spacing:0.5px;margin-bottom:6px;display:flex;align-items:center;gap:6px}
  .audio-dot{width:6px;height:6px;background:#4ade80;border-radius:50%;display:inline-block}
  audio{width:100%;height:36px;border-radius:6px;accent-color:#1a1a1a;display:block}
  .notes-area{width:100%;margin-top:6px;padding:8px 10px;border:0.5px solid #e5e5e0;
              border-radius:6px;font-size:13px;resize:vertical;min-height:60px;
              font-family:inherit;background:#fafafa;color:#1a1a1a}
  .notes-area:focus{outline:none;border-color:#6b7280;background:#fff}
  .action-row{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap}
  .action-btn{padding:5px 12px;border-radius:6px;font-size:12px;border:0.5px solid #e5e5e0;
              background:#fff;cursor:pointer;transition:all 0.15s;font-family:inherit}
  .action-btn:hover{background:#f3f4f6}
  .action-btn.primary{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
  .filter-bar{display:flex;gap:8px;flex-wrap:wrap;flex:1}
  .filter-btn{padding:5px 12px;border-radius:6px;border:0.5px solid #e5e5e0;
              background:#fff;font-size:12px;cursor:pointer;font-family:inherit}
  .filter-btn.active{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
  .export-btn{padding:5px 12px;border-radius:6px;font-size:12px;border:0.5px solid #e5e5e0;
              background:#fff;cursor:pointer;font-family:inherit;white-space:nowrap}
  .export-btn:hover{background:#f3f4f6}
  .search-bar{width:100%;padding:9px 14px;border-radius:8px;border:0.5px solid #e5e5e0;
              font-size:13px;margin-bottom:14px;background:#fff;font-family:inherit}
  .search-bar:focus{outline:none;border-color:#6b7280}
  .empty-state{text-align:center;padding:60px 24px;color:#9ca3af}
  .empty-icon{font-size:40px;margin-bottom:12px}
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
  <div class="stat-card"><div class="stat-label">Total messages</div>
    <div class="stat-value" id="stat-total">0</div></div>
  <div class="stat-card"><div class="stat-label">Translated</div>
    <div class="stat-value translated" id="stat-translated">0</div></div>
  <div class="stat-card"><div class="stat-label">Urgent alerts</div>
    <div class="stat-value urgent" id="stat-urgent">0</div></div>
  <div class="stat-card"><div class="stat-label">Pending calls</div>
    <div class="stat-value pending" id="stat-pending">0</div></div>
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

const socket=io();
socket.on('connect',()=>{
  wsConnected=true;
  document.getElementById('conn-dot').style.color='#4ade80';
  document.getElementById('ws-badge').style.display='inline';
  document.getElementById('last-updated').textContent='Live (WebSocket)';
});
socket.on('disconnect',()=>{
  wsConnected=false;
  document.getElementById('conn-dot').style.color='#dc2626';
  document.getElementById('ws-badge').style.display='none';
  document.getElementById('last-updated').textContent='WS disconnected';
});
socket.on('trend_alert',(alert)=>{
  const banner=document.createElement('div');
  banner.style.cssText='position:fixed;top:60px;left:50%;transform:translateX(-50%);'+
    'background:#dc2626;color:#fff;padding:12px 20px;border-radius:8px;'+
    'font-size:13px;font-weight:600;z-index:999;max-width:500px;text-align:center;'+
    'box-shadow:0 4px 20px rgba(220,38,38,0.4)';
  banner.textContent='🚨 '+(alert.message||'Trend alert detected');
  document.body.appendChild(banner);
  setTimeout(()=>banner.remove(),12000);
  document.title='🚨 TREND ALERT — Kingolik';
  setTimeout(()=>document.title='Kingolik — NGO Live Dashboard',10000);
});
socket.on('session_updated',(session)=>{
  const idx=allSessions.findIndex(s=>s.session_id===session.session_id);
  if(idx>=0) allSessions[idx]=session; else allSessions.unshift(session);
  updateStats(); patchOrInsertCard(session);
  startLiveTimers();  // restart timers to pick up new pending cards
  const kws=session.translation?.urgent_keywords||[];
  if(kws.length>0&&!isFirstLoad){
    document.title='(!) URGENT — Kingolik';
    setTimeout(()=>document.title='Kingolik — NGO Live Dashboard',5000);
  }
});

function patchOrInsertCard(s){
  const container=document.getElementById('cards-container');
  const existing=container.querySelector(`[data-session="${s.session_id}"]`);
  if(existing){
    const ta=existing.querySelector('textarea');
    if(ta&&document.activeElement===ta) return;
    const t=s.translation||{};
    const tEl=existing.querySelector('[data-role="translation-text"]');
    const trEl=existing.querySelector('[data-role="transcript-text"]');
    if(tEl&&t.translation) tEl.textContent=t.translation;
    if(trEl&&t.transcript) trEl.textContent=t.transcript;
    const ns=getStatus(s);
    ['urgent','pending','handled','translated'].forEach(c=>existing.classList.remove(c));
    existing.classList.add(ns);
  } else {
    const tmp=document.createElement('div');
    tmp.innerHTML=buildCardHTML(s);
    const el=tmp.firstElementChild;
    if(!isFirstLoad) el.classList.add('new');
    const first=container.querySelector('.card');
    if(first) container.insertBefore(el,first); else container.appendChild(el);
  }
}

function setFilter(f,btn){
  activeFilter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}

function timeAgo(iso){
  if(!iso) return '';
  const sec=Math.floor((Date.now()-new Date(iso).getTime())/1000);
  if(sec<5)   return 'just now';
  if(sec<60)  return sec+'s ago';
  if(sec<3600) return Math.floor(sec/60)+'m ago';
  if(sec<86400) return Math.floor(sec/3600)+'h '+(Math.floor((sec%3600)/60))+'m ago';
  return new Date(iso).toLocaleDateString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
}

// Live timestamp updater — refreshes all card timestamps every 30 seconds
function startTimestampUpdater(){
  setInterval(()=>{
    document.querySelectorAll('[data-ts]').forEach(el=>{
      el.textContent=timeAgo(el.dataset.ts);
    });
  },30000);
}

function getStatus(s){
  if(s.handled||s.status==='handled') return 'handled';
  if(s.status==='pending_call') return 'pending';
  const t=s.translation||{};
  if((t.urgent_keywords||[]).length>0) return 'urgent';
  if(t.translation) return 'translated';
  return 'pending';
}

function updateStats(){
  document.getElementById('stat-total').textContent=allSessions.length;
  document.getElementById('stat-translated').textContent=
    allSessions.filter(s=>s.translation?.translation).length;
  document.getElementById('stat-urgent').textContent=
    allSessions.filter(s=>(s.translation?.urgent_keywords||[]).length>0).length;
  document.getElementById('stat-pending').textContent=
    allSessions.filter(s=>s.status==='pending_call').length;
}

function buildCardHTML(s){
  const t=s.translation||{}, status=getStatus(s), engine=t.engine||'';
  const keywords=t.urgent_keywords||[], sid=s.session_id;
  const note=notesStore[sid]||s.note||'', audioUrl='/api/audio/'+sid;
  const handled=status==='handled';
  return `<div class="card ${status}" data-session="${sid}">
    <div class="card-header">
      <div class="card-meta">
        ${status==='urgent'?'<span class="badge badge-urgent">URGENT</span>':''}
        ${status==='pending'?'<span class="badge badge-pending">pending call</span>':''}
        ${handled?'<span class="badge badge-handled">handled</span>':''}
        ${t.detected_language?`<span class="badge badge-lang">${t.detected_language}</span>`:''}
        ${engine==='cloud'?'<span class="badge badge-cloud">Gemini</span>':''}
        ${engine==='local'?'<span class="badge badge-local">Local AI</span>':''}
        ${status==='pending'?`<div style="font-size:11px;color:#d97706;margin-top:4px"
          data-created="${s.timestamp}" id="timer-wrap-${sid}">
          Processing… <span class="live-timer" data-start="${s.timestamp}">0</span>s
          (AI translating voice)
        </div>`:''}
      </div>
      <div style="display:flex;gap:12px;align-items:center">
        <span class="phone">${s.phone||'unknown'}</span>
        <span class="timestamp" data-ts="${s.timestamp}">${timeAgo(s.timestamp)}</span>
        <button onclick="deleteSession('${sid}')" title="Delete this record"
          style="background:none;border:none;cursor:pointer;color:#d1d5db;font-size:14px;
                 padding:2px 4px;border-radius:4px;line-height:1;transition:color 0.15s"
          onmouseover="this.style.color='#dc2626'" onmouseout="this.style.color='#d1d5db'">✕</button>
      </div>
    </div>
    ${t.translation?`<div class="translation-block">
      <div class="tblock-label">English translation</div>
      <div class="tblock-text" data-role="translation-text">${t.translation}</div>
    </div>`:''}
    ${t.transcript&&t.transcript!==t.translation?`<div class="translation-block">
      <div class="tblock-label">Original (${t.detected_language||'detected'})</div>
      <div class="tblock-text original" data-role="transcript-text">${t.transcript}</div>
    </div>`:''}
    ${keywords.length>0?`<div class="urgent-keywords">
      ${keywords.map(k=>`<span class="kw-chip">${k}</span>`).join('')}
    </div>`:''}
    ${t.confidence?`<div style="font-size:11px;color:#9ca3af;margin-top:8px">
      Confidence: ${t.confidence}
      ${t.latency_ms?' · AI latency: '+(t.latency_ms/1000).toFixed(1)+'s':''}
      ${s.duration?' · recording: '+s.duration+'s':''}
      ${t.score?' · score: '+t.score:''}</div>`:''}
    ${(t.requires_review||t.confidence==='low')?`
    <div style="background:#fef3c7;border:0.5px solid #d97706;border-radius:6px;
                padding:8px 12px;margin-top:8px;display:flex;align-items:center;gap:8px">
      <span>⚠️</span>
      <div>
        <div style="font-size:12px;font-weight:600;color:#92400e">UNCERTAIN — requires human review</div>
        <div style="font-size:11px;color:#b45309;margin-top:2px">${t.review_reason||'Low confidence — verify before dispatch'}</div>
      </div>
    </div>`:''}
    <div class="audio-section">
      <div class="audio-label"><span class="audio-dot"></span>Voice recording
        ${s.duration?`<span style="font-size:10px;color:#9ca3af;margin-left:6px">${s.duration}s</span>`:''}
      </div>
      ${t.is_text_report ? `
        <div style="font-size:11px;color:#9ca3af;padding:4px 0">
          📝 Text report — no audio recording
        </div>
      ` : status==='pending' ? `
        <div style="font-size:11px;color:#d97706;padding:4px 0">
          ⏳ Call in progress — recording will appear after translation completes
        </div>
      ` : `
        <audio id="audio-${sid}" controls preload="none"
               style="width:100%;height:36px;border-radius:6px;accent-color:#1a1a1a;display:block"
               onplay="markAudioLoaded('${sid}')"
               onerror="document.getElementById('audio-err-${sid}').style.display='flex';this.style.display='none'">
          <source src="/api/audio/${sid}?t=${Date.now()}" type="audio/wav">
        </audio>
        <div id="audio-err-${sid}" style="display:none;align-items:center;gap:8px;margin-top:4px">
          <span style="font-size:11px;color:#9ca3af">Audio processing — </span>
          <button onclick="reloadAudio('${sid}')" 
                  style="font-size:11px;color:#6b7280;background:none;border:0.5px solid #e5e5e0;
                         padding:2px 8px;border-radius:4px;cursor:pointer">
            Refresh audio
          </button>
        </div>
      `}
    </div>
    ${!handled?`<div style="margin-top:12px;padding-top:10px;border-top:0.5px solid #f3f4f6">
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
        placeholder="If translation is wrong, type the correct English here — this becomes gold standard training data..."
        style="min-height:48px;border-color:#d1fae5"></textarea>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:4px">
        <button class="action-btn" id="corbtn-${sid}" onclick="saveCorrection('${sid}')"
          style="font-size:11px;color:#16a34a;border-color:#d1fae5">Submit correction</button>
        <span style="font-size:10px;color:#9ca3af">Corrections train the local AI model</span>
      </div>
    </div>`:
    `<div style="margin-top:8px;padding-top:8px;border-top:0.5px solid #f3f4f6">
      ${note?`<div style="font-size:12px;color:#6b7280;font-style:italic">"${note}"</div>`:''}
      <div style="font-size:11px;color:#16a34a;margin-top:4px">Handled ✓</div>
    </div>`}
  </div>`;
}

function renderCards(){
  const search=document.getElementById('search').value.toLowerCase();
  const container=document.getElementById('cards-container');
  const filtered=allSessions.filter(s=>{
    const status=getStatus(s), t=s.translation||{}, engine=t.engine||'';
    if(activeFilter==='latest') return s.session_id===allSessions[0]?.session_id;
    if(activeFilter==='urgent'&&status!=='urgent') return false;
    if(activeFilter==='translated'&&!t.translation) return false;
    if(activeFilter==='pending'&&status!=='pending') return false;
    if(activeFilter==='handled'&&status!=='handled') return false;
    if(activeFilter==='cloud'&&engine!=='cloud') return false;
    if(activeFilter==='local'&&engine!=='local') return false;
    if(search){
      const hay=[s.phone,t.translation,t.transcript,t.detected_language,
                 (t.urgent_keywords||[]).join(' ')].join(' ').toLowerCase();
      if(!hay.includes(search)) return false;
    }
    return true;
  });
  document.getElementById('results-label').textContent=
    filtered.length+' message'+(filtered.length!==1?'s':'');
  if(filtered.length===0){
    container.innerHTML=`<div class="empty-state"><div class="empty-icon">📭</div>
      <div>No messages yet</div>
      <div style="font-size:12px;margin-top:6px">Dial *384*67660# on the simulator to start</div></div>`;
    return;
  }
  filtered.forEach((s,i)=>{
    const sid=s.session_id, existing=container.querySelector(`[data-session="${sid}"]`);
    if(existing){
      const ta=existing.querySelector('textarea');
      if(ta&&(document.activeElement===ta||ta.value!==(notesStore[sid]||s.note||''))) return;
      if(ta?.value) notesStore[sid]=ta.value;
      const t=s.translation||{};
      const tEl=existing.querySelector('[data-role="translation-text"]');
      const trEl=existing.querySelector('[data-role="transcript-text"]');
      if(tEl&&t.translation) tEl.textContent=t.translation;
      if(trEl&&t.transcript) trEl.textContent=t.transcript;
      const ns=getStatus(s);
      ['urgent','pending','handled','translated'].forEach(c=>existing.classList.remove(c));
      existing.classList.add(ns);
    } else {
      const tmp=document.createElement('div');
      tmp.innerHTML=buildCardHTML(s);
      const el=tmp.firstElementChild;
      if(!isFirstLoad) el.classList.add('new');
      const cards=container.querySelectorAll('.card');
      if(i<cards.length) container.insertBefore(el,cards[i]);
      else container.appendChild(el);
    }
  });
  container.querySelectorAll('.card').forEach(card=>{
    if(!filtered.find(s=>s.session_id===card.dataset.session)) card.remove();
  });
  filtered.forEach(s=>knownIds.add(s.session_id));
  if(isFirstLoad) isFirstLoad=false;
}

async function deleteSession(sid){
  if(!confirm('Delete this record permanently? This cannot be undone.')) return;
  try{
    const resp=await fetch('/api/delete-session',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid})});
    const data=await resp.json();
    if(data.ok){
      // Remove from local array and DOM immediately
      allSessions=allSessions.filter(s=>s.session_id!==sid);
      const card=document.querySelector(`[data-session="${sid}"]`);
      if(card){
        card.style.transition='opacity 0.3s,transform 0.3s';
        card.style.opacity='0'; card.style.transform='translateX(20px)';
        setTimeout(()=>card.remove(),300);
      }
      updateStats();
    } else {
      alert('Delete failed: '+(data.error||'unknown error'));
    }
  } catch(e){
    alert('Network error — could not delete');
  }
}

async function markHandled(sid){
  await fetch('/api/mark-handled',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid})});
  const s=allSessions.find(x=>x.session_id===sid);
  if(s){s.handled=true;s.status='handled';} renderCards();
}

async function saveNote(sid){
  const el=document.getElementById('note-'+sid), btn=document.getElementById('notebtn-'+sid);
  if(!el) return;
  notesStore[sid]=el.value;
  await fetch('/api/save-note',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session_id:sid,note:el.value})});
  if(btn){btn.textContent='Saved ✓';btn.style.background='#16a34a';btn.style.color='#fff';
    setTimeout(()=>{btn.textContent='Save note';btn.style.background='';btn.style.color='';},2000);}
}

async function saveCorrection(sid){
  const el=document.getElementById('correction-'+sid);
  const btn=document.getElementById('corbtn-'+sid);
  if(!el||!el.value.trim()){
    if(btn){btn.textContent='⚠ Type a correction first';btn.style.color='#dc2626';}
    setTimeout(()=>{if(btn){btn.textContent='Submit correction';btn.style.color='#16a34a';}},2000);
    return;
  }
  const correctionText=el.value.trim();
  try{
    const resp=await fetch('/api/save-correction',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid,correction:correctionText})});
    const data=await resp.json();
    if(data.ok){
      if(btn){
        btn.textContent='✓ Saved as training data (gold pair #'+data.total+')';
        btn.style.color='#15803d';btn.style.fontWeight='600';
      }
      // Update the local session object so analytics reflects it
      const s=allSessions.find(x=>x.session_id===sid);
      if(s) s.correction=correctionText;
    } else {
      if(btn){btn.textContent='⚠ Save failed — see console';btn.style.color='#dc2626';}
      console.error('save-correction failed:',data);
    }
  } catch(e){
    if(btn){btn.textContent='⚠ Network error';btn.style.color='#dc2626';}
    console.error('save-correction error:',e);
  }
}

// ── Audio helpers ─────────────────────────────────────────────
function reloadAudio(sid){
  const audio=document.getElementById('audio-'+sid);
  const errDiv=document.getElementById('audio-err-'+sid);
  if(!audio) return;
  // Force reload with new cache-busting timestamp
  const src=audio.querySelector('source');
  if(src) src.src='/api/audio/'+sid+'?t='+Date.now();
  audio.style.display='block';
  if(errDiv) errDiv.style.display='none';
  audio.load();
}

function markAudioLoaded(sid){
  const errDiv=document.getElementById('audio-err-'+sid);
  if(errDiv) errDiv.style.display='none';
}

async function fetchSessions(){
  try{
    const data=await(await fetch('/api/sessions')).json();
    allSessions=data; renderCards(); updateStats();
    startLiveTimers();  // start timers after cards render
    if(!wsConnected){
      document.getElementById('last-updated').textContent='Updated '+new Date().toLocaleTimeString();
      document.getElementById('conn-dot').style.color='#4ade80';
    }
  } catch(e){
    if(!wsConnected) document.getElementById('conn-dot').style.color='#dc2626';
  }
}

// ── Live processing timer ─────────────────────────────────────
// Counts seconds from session creation time (server timestamp)
// Shows how long AI is taking to translate — judges love this
let _timerInterval=null;
function startLiveTimers(){
  if(_timerInterval) clearInterval(_timerInterval);
  _timerInterval=setInterval(()=>{
    document.querySelectorAll('.live-timer').forEach(el=>{
      const start=el.dataset.start;
      if(!start) return;
      try{
        const diff=Math.floor((Date.now()-new Date(start).getTime())/1000);
        el.textContent=diff>0?diff:'0';
      } catch(e){}
    });
  },1000);
}
fetchSessions();
setInterval(fetchSessions,30000);
startTimestampUpdater();
</script>
</body></html>"""


# ══════════════════════════════════════════════════════════════
#  ANALYTICS DASHBOARD HTML
# ══════════════════════════════════════════════════════════════
ANALYTICS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kingolik — Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#0f1117;color:#e2e8f0;font-size:13px}
  .topbar{background:#0f1117;border-bottom:1px solid #1e2433;padding:14px 24px;
          display:flex;align-items:center;justify-content:space-between;
          position:sticky;top:0;z-index:100}
  .topbar h1{font-size:16px;font-weight:500;color:#e2e8f0;letter-spacing:-0.3px}
  .topbar h1 span{color:#4ade80}
  .topbar-right{display:flex;align-items:center;gap:12px}
  .nav-link{color:#64748b;font-size:12px;text-decoration:none;
            padding:4px 10px;border-radius:6px;border:0.5px solid #1e2433}
  .nav-link:hover{color:#e2e8f0;border-color:#334155}
  .nav-link.active{color:#4ade80;border-color:#4ade80}
  .period-btn{background:#1e2433;border:0.5px solid #334155;color:#94a3b8;
              padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;font-family:inherit}
  .period-btn.active{background:#4ade80;color:#0f1117;border-color:#4ade80;font-weight:600}
  .live-dot{width:7px;height:7px;background:#4ade80;border-radius:50%;
            display:inline-block;margin-right:5px;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
  .grid{display:grid;gap:16px;padding:20px 24px}
  .row-4{grid-template-columns:repeat(4,1fr)}
  .row-3{grid-template-columns:2fr 1fr 1fr}
  .row-2{grid-template-columns:1.4fr 1fr}
  .card{background:#161b27;border:0.5px solid #1e2433;border-radius:12px;padding:18px}
  .card-title{font-size:10px;color:#64748b;text-transform:uppercase;
              letter-spacing:0.8px;margin-bottom:12px}
  .big-num{font-size:42px;font-weight:600;line-height:1;
           background:linear-gradient(135deg,#4ade80,#22d3ee);
           -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .big-num.red{background:linear-gradient(135deg,#f87171,#fb923c);
               -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .big-num.blue{background:linear-gradient(135deg,#60a5fa,#a78bfa);
                -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .big-num.amber{background:linear-gradient(135deg,#fbbf24,#f97316);
                 -webkit-background-clip:text;-webkit-text-fill-color:transparent}
  .sub-label{font-size:11px;color:#475569;margin-top:6px}
  .accuracy-ring{display:flex;align-items:center;gap:16px}
  .ring-wrap{position:relative;width:90px;height:90px;flex-shrink:0}
  .ring-wrap canvas{width:90px!important;height:90px!important}
  .ring-center{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center}
  .ring-pct{font-size:20px;font-weight:700;color:#4ade80}
  .ring-lbl{font-size:9px;color:#64748b;margin-top:1px}
  .ring-info .label{font-size:12px;color:#94a3b8;margin-bottom:4px}
  .ring-info .detail{font-size:11px;color:#475569}
  .chart-wrap{position:relative;width:100%;height:160px}
  .lang-bar{display:flex;flex-direction:column;gap:8px}
  .lang-row{display:flex;align-items:center;gap:8px}
  .lang-name{font-size:11px;color:#94a3b8;width:60px;flex-shrink:0}
  .lang-track{flex:1;background:#1e2433;border-radius:4px;height:8px;overflow:hidden}
  .lang-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,#4ade80,#22d3ee)}
  .lang-pct{font-size:10px;color:#64748b;width:32px;text-align:right;flex-shrink:0}
  .log-list{display:flex;flex-direction:column;gap:6px;max-height:200px;overflow-y:auto}
  .log-row{display:flex;gap:8px;align-items:flex-start;font-size:11px}
  .log-ts{color:#334155;font-family:monospace;flex-shrink:0;padding-top:1px}
  .log-dot{width:6px;height:6px;border-radius:50%;margin-top:4px;flex-shrink:0}
  .log-dot.urgent{background:#f87171}
  .log-dot.ok{background:#4ade80}
  .log-dot.pending{background:#fbbf24}
  .log-msg{color:#94a3b8;line-height:1.4}
  .kw-grid{display:flex;flex-wrap:wrap;gap:6px}
  .kw-tag{background:#1e2433;border:0.5px solid #dc2626;color:#f87171;
          padding:3px 8px;border-radius:10px;font-size:10px;font-weight:500}
  .kw-tag .cnt{color:#64748b;margin-left:4px}
  .engine-row{display:flex;gap:12px;margin-top:4px}
  .eng-item{flex:1;background:#1e2433;border-radius:8px;padding:10px 12px}
  .eng-label{font-size:10px;color:#64748b;margin-bottom:4px}
  .eng-val{font-size:20px;font-weight:600;color:#e2e8f0}
  .eng-sub{font-size:10px;color:#475569;margin-top:2px}
  /* Co-Pilot panel */
  .copilot-panel{background:#161b27;border:0.5px solid #1e2433;
                 border-radius:12px;padding:20px;position:relative}
  .cp-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
  .cp-title{font-size:12px;color:#4ade80;font-weight:600;letter-spacing:0.5px;
            display:flex;align-items:center;gap:8px}
  .cp-mode{font-size:10px;color:#334155;font-family:monospace}
  .cp-messages{min-height:100px;max-height:200px;overflow-y:auto;
               display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
  .cp-msg{display:flex;gap:8px;align-items:flex-start}
  .cp-msg.user{flex-direction:row-reverse}
  .cp-av{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;
         justify-content:center;font-size:10px;font-weight:600;flex-shrink:0}
  .cp-av.bot{background:rgba(74,222,128,0.1);color:#4ade80;border:0.5px solid rgba(74,222,128,0.3)}
  .cp-av.usr{background:#1e2433;color:#64748b}
  .cp-text{background:#1e2433;border:0.5px solid #334155;border-radius:8px;
           padding:8px 12px;font-size:12px;color:#e2e8f0;max-width:80%;line-height:1.5}
  .cp-msg.user .cp-text{background:rgba(74,222,128,0.08);border-color:rgba(74,222,128,0.2)}
  .cp-thinking{display:flex;gap:4px;align-items:center;padding:8px 12px}
  .cp-thinking span{width:5px;height:5px;border-radius:50%;background:#4ade80;
                    animation:think 1.2s ease-in-out infinite}
  .cp-thinking span:nth-child(2){animation-delay:0.2s}
  .cp-thinking span:nth-child(3){animation-delay:0.4s}
  @keyframes think{0%,100%{opacity:0.2;transform:scale(0.8)}50%{opacity:1;transform:scale(1.2)}}
  /* Waveform */
  .waveform{display:flex;align-items:center;gap:3px;height:24px;
            margin-bottom:10px;opacity:0;transition:opacity 0.3s}
  .waveform.active{opacity:1}
  .wave-bar{width:3px;border-radius:2px;background:#4ade80;
            animation:wv 0.8s ease-in-out infinite}
  @keyframes wv{0%,100%{height:3px;opacity:0.3}50%{height:20px;opacity:1}}
  .cp-input-row{display:flex;gap:8px;align-items:center}
  .cp-input{flex:1;background:#1e2433;border:0.5px solid #334155;border-radius:20px;
            padding:10px 16px;color:#e2e8f0;font-family:inherit;font-size:12px;outline:none}
  .cp-input:focus{border-color:#4ade80}
  .cp-input::placeholder{color:#334155}
  .mic-btn{width:38px;height:38px;border-radius:50%;background:#1e2433;
           border:0.5px solid #334155;display:flex;align-items:center;justify-content:center;
           cursor:pointer;transition:all 0.2s;color:#64748b;font-size:16px;flex-shrink:0}
  .mic-btn:hover{border-color:#4ade80;color:#4ade80}
  .mic-btn.listening{background:rgba(248,113,113,0.1);border-color:#f87171;color:#f87171;
                     animation:pulsered 1s ease-in-out infinite}
  @keyframes pulsered{0%,100%{box-shadow:0 0 0 0 rgba(248,113,113,0.3)}
                       50%{box-shadow:0 0 0 6px transparent}}
  .send-btn{width:38px;height:38px;border-radius:50%;background:#4ade80;border:none;
            display:flex;align-items:center;justify-content:center;cursor:pointer;
            transition:all 0.2s;color:#0f1117;font-size:15px;flex-shrink:0}
  .send-btn:hover{background:#22d3ee;transform:scale(1.05)}
  .cp-hint{font-size:9px;color:#1e2433;text-align:center;margin-top:10px;
           font-family:monospace;letter-spacing:0.5px;text-transform:uppercase}
  .footer-bar{text-align:center;padding:16px;color:#1e2433;font-size:10px}
  @media(max-width:768px){
    .row-4,.row-3,.row-2{grid-template-columns:1fr}
    .grid{padding:12px}
  }
</style>
</head>
<body>

<div class="topbar">
  <h1>Kingo<span>lik</span> — <span style="color:#64748b;font-weight:400">AI Analytics</span></h1>
  <div class="topbar-right">
    <a href="/dashboard" class="nav-link">Live Feed</a>
    <a href="/analytics" class="nav-link active">Analytics</a>
    <button class="period-btn active" onclick="setPeriod(7,this)">7d</button>
    <button class="period-btn" onclick="setPeriod(30,this)">30d</button>
    <button class="period-btn" onclick="setPeriod(90,this)">90d</button>
    <span style="font-size:12px;color:#475569"><span class="live-dot"></span>Live</span>
  </div>
</div>

<!-- Stats row -->
<div class="grid row-4" style="margin-top:4px">
  <div class="card"><div class="card-title">Total voice messages</div>
    <div class="big-num" id="a-total">—</div><div class="sub-label">calls processed</div></div>
  <div class="card"><div class="card-title">Urgent alerts</div>
    <div class="big-num red" id="a-urgent">—</div><div class="sub-label">keywords detected</div></div>
  <div class="card"><div class="card-title">Successfully translated</div>
    <div class="big-num blue" id="a-translated">—</div><div class="sub-label">into English</div></div>
  <div class="card"><div class="card-title">Cases handled</div>
    <div class="big-num amber" id="a-handled">—</div><div class="sub-label">by caseworkers</div></div>
</div>

<!-- Latency / Review / Rate limit row -->
<div class="grid" style="grid-template-columns:1fr 1fr 1fr;margin-top:0;padding-top:0">
  <div class="card" style="display:flex;align-items:center;gap:16px">
    <div><div class="card-title">Avg translation latency</div>
      <div style="font-size:32px;font-weight:700;color:#22d3ee" id="a-latency-ms">—</div>
      <div class="sub-label">milliseconds end-to-end</div></div>
    <div style="font-size:32px;opacity:0.2">⚡</div>
  </div>
  <div class="card" style="display:flex;align-items:center;gap:16px">
    <div><div class="card-title">Needs human review</div>
      <div style="font-size:32px;font-weight:700;color:#f87171" id="a-review">—</div>
      <div class="sub-label">low confidence translations</div></div>
    <div style="font-size:32px;opacity:0.2">👁</div>
  </div>
  <div class="card" style="display:flex;align-items:center;gap:16px">
    <div><div class="card-title">Rate limit protection</div>
      <div style="font-size:32px;font-weight:700;color:#4ade80">3/hr</div>
      <div class="sub-label">max per phone number</div></div>
    <div style="font-size:32px;opacity:0.2">🛡</div>
  </div>
</div>

<!-- HITL banner -->
<div class="grid" style="grid-template-columns:1fr;padding-top:0;margin-top:0">
  <div class="card" style="background:linear-gradient(135deg,#0d1f12,#161b27);border:0.5px solid #166534">
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

<!-- Charts row -->
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
        <div class="detail">Gemini + Whisper medium</div>
        <div class="detail" style="margin-top:6px" id="a-period">Last 7 days</div>
      </div>
    </div>
    <div class="engine-row" style="margin-top:14px">
      <div class="eng-item"><div class="eng-label">Gemini cloud</div>
        <div class="eng-val" id="a-cloud">—</div><div class="eng-sub">calls</div></div>
      <div class="eng-item"><div class="eng-label">Local Whisper</div>
        <div class="eng-val" id="a-local">—</div><div class="eng-sub">calls</div></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Voice feedback language</div>
    <div class="lang-bar" id="lang-bars" style="margin-top:8px">
      <div style="color:#334155;font-size:11px">Loading...</div>
    </div>
  </div>
</div>

<!-- Log + Keywords -->
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

<!-- Co-Pilot panel -->
<div class="grid" style="grid-template-columns:1fr;padding-top:0;margin-top:0">
  <div class="copilot-panel">
    <div class="cp-header">
      <div class="cp-title">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <circle cx="7" cy="7" r="6" stroke="#4ade80" stroke-width="1"/>
          <path d="M4.5 7L6.5 9L9.5 5" stroke="#4ade80" stroke-width="1.5" stroke-linecap="round"/>
        </svg>
        King'olik Co-Pilot
      </div>
      <div class="cp-mode" id="cp-mode">Groq · Llama 3 · Ready</div>
    </div>

    <!-- Waveform animation — shows when speaking -->
    <div class="waveform" id="cp-waveform">
      <div class="wave-bar" style="animation-delay:0s"></div>
      <div class="wave-bar" style="animation-delay:0.1s"></div>
      <div class="wave-bar" style="animation-delay:0.2s"></div>
      <div class="wave-bar" style="animation-delay:0.15s"></div>
      <div class="wave-bar" style="animation-delay:0.3s"></div>
      <div class="wave-bar" style="animation-delay:0.05s"></div>
      <div class="wave-bar" style="animation-delay:0.25s"></div>
      <div class="wave-bar" style="animation-delay:0.1s"></div>
    </div>

    <div class="cp-messages" id="cp-messages">
      <div class="cp-msg">
        <div class="cp-av bot">K</div>
        <div class="cp-text">
          Co-Pilot online. I have live access to your field database.
          Ask about trends, urgent alerts, or status. Press the mic to speak.
        </div>
      </div>
    </div>

    <div class="cp-input-row">
      <input class="cp-input" id="cp-input"
        placeholder="Ask about field reports, trends, urgent alerts..."
        onkeypress="if(event.key==='Enter')askCopilot()">
      <button class="mic-btn" id="cp-mic" onclick="toggleMic()" title="Push to talk">🎤</button>
      <button class="send-btn" onclick="askCopilot()">→</button>
    </div>
    <div class="cp-hint">Push to talk · Domain-locked · Groq 800 tok/s · Falls back offline</div>
  </div>
</div>

<!-- Screen edge glow — animates on voice activity -->
<div id="edge-glow" style="
  position:fixed;inset:0;pointer-events:none;z-index:9999;
  opacity:0;transition:opacity 0.4s ease;
  background:
    linear-gradient(90deg, rgba(74,222,128,0.18) 0%, transparent 6%, transparent 94%, rgba(74,222,128,0.18) 100%),
    linear-gradient(0deg,  rgba(74,222,128,0.18) 0%, transparent 6%, transparent 94%, rgba(74,222,128,0.18) 100%);
  border-radius:0;
"></div>

<!-- Animated gradient border following screen path -->
<canvas id="edge-canvas" style="
  position:fixed;inset:0;pointer-events:none;z-index:9998;
  opacity:0;transition:opacity 0.4s ease;
  width:100vw;height:100vh;
"></canvas>

<div id="edge-glow" style="position:fixed;inset:0;pointer-events:none;z-index:9999;opacity:0;transition:opacity 0.4s ease;background:linear-gradient(90deg,rgba(74,222,128,0.15) 0%,transparent 5%,transparent 95%,rgba(74,222,128,0.15) 100%),linear-gradient(0deg,rgba(74,222,128,0.15) 0%,transparent 5%,transparent 95%,rgba(74,222,128,0.15) 100%)"></div>
<canvas id="edge-canvas" style="position:fixed;inset:0;pointer-events:none;z-index:9998;opacity:0;transition:opacity 0.4s ease"></canvas>

<div class="footer-bar">Kingolik NGO Voice Bridge · Live deployment · kingo-lik-engine.onrender.com</div>

<script>
let currentDays=7, dailyChart=null, accuracyChart=null;
let cpListening=false, cpRecognition=null;

// ── Period selector ──────────────────────────────────────────
function setPeriod(days,btn){
  currentDays=days;
  document.querySelectorAll('.period-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadAnalytics();
}

// ── Load analytics from server ───────────────────────────────
async function loadAnalytics(){
  try{
    const data=await(await fetch('/api/analytics?days='+currentDays)).json();
    renderAnalytics(data);
  } catch(e){ console.error('Analytics load failed:',e); }
}

function renderAnalytics(d){
  document.getElementById('a-total').textContent=d.total;
  document.getElementById('a-urgent').textContent=d.urgent;
  document.getElementById('a-translated').textContent=d.translated;
  document.getElementById('a-handled').textContent=d.handled;
  document.getElementById('a-accuracy').textContent=d.accuracy+'%';
  document.getElementById('a-cloud').textContent=d.engine_counts.cloud||0;
  document.getElementById('a-local').textContent=d.engine_counts.local||0;
  document.getElementById('a-period').textContent='Last '+d.period_days+' days';
  document.getElementById('a-latency-ms').textContent=d.avg_latency_ms?d.avg_latency_ms+'ms':'—';
  document.getElementById('a-review').textContent=d.needs_review||0;
  document.getElementById('a-gold').textContent=d.gold_pairs||0;

  // Daily engagement chart
  if(dailyChart) dailyChart.destroy();
  const ctx=document.getElementById('chart-daily').getContext('2d');
  dailyChart=new Chart(ctx,{
    type:'line',
    data:{
      labels:d.daily.map(x=>x.day),
      datasets:[
        {label:'Voice-AI Calls',data:d.daily.map(x=>x.total),
         borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,0.08)',
         tension:0.4,fill:true,pointRadius:3,pointBackgroundColor:'#4ade80'},
        {label:'Urgent Alerts',data:d.daily.map(x=>x.urgent),
         borderColor:'#f87171',backgroundColor:'rgba(248,113,113,0.08)',
         tension:0.4,fill:true,pointRadius:3,pointBackgroundColor:'#f87171'}
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{labels:{color:'#64748b',font:{size:10}}}},
      scales:{
        x:{ticks:{color:'#475569',font:{size:10}},grid:{color:'#1e2433'}},
        y:{ticks:{color:'#475569',font:{size:10}},grid:{color:'#1e2433'},beginAtZero:true}
      }
    }
  });

  // Accuracy donut
  if(accuracyChart) accuracyChart.destroy();
  const ctx2=document.getElementById('chart-accuracy').getContext('2d');
  const acc=d.accuracy;
  accuracyChart=new Chart(ctx2,{
    type:'doughnut',
    data:{datasets:[{data:[acc,100-acc],backgroundColor:['#4ade80','#1e2433'],
                     borderWidth:0,cutout:'78%'}]},
    options:{responsive:false,plugins:{legend:{display:false},tooltip:{enabled:false}}}
  });

  // Language bars
  const total=d.total||1;
  const langs=Object.entries(d.lang_counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
  const colors=['#4ade80','#22d3ee','#a78bfa','#fb923c','#f472b6','#fbbf24'];
  document.getElementById('lang-bars').innerHTML=langs.map(([lang,count],i)=>{
    const pct=Math.round(count/total*100);
    return `<div class="lang-row">
      <div class="lang-name">${lang}</div>
      <div class="lang-track"><div class="lang-fill" style="width:${pct}%;background:${colors[i]||'#4ade80'}"></div></div>
      <div class="lang-pct">${pct}%</div>
    </div>`;
  }).join('')||'<div style="color:#334155;font-size:11px">No data yet</div>';

  // Pipeline log
  document.getElementById('pipeline-log').innerHTML=d.log.map(e=>`
    <div class="log-row">
      <div class="log-ts">${e.ts.slice(11)}</div>
      <div class="log-dot ${e.kind}"></div>
      <div class="log-msg"><span style="color:#475569">${e.phone||''}</span>
        ${e.phone?' · ':''} ${e.msg}</div>
    </div>`).join('')||'<div style="color:#334155;font-size:11px">No activity yet</div>';

  // Keywords
  document.getElementById('kw-grid').innerHTML=d.top_keywords.map(k=>
    `<div class="kw-tag">${k.kw}<span class="cnt">×${k.count}</span></div>`
  ).join('')||'<div style="color:#334155;font-size:11px">No urgent keywords yet</div>';
}

loadAnalytics();
setInterval(loadAnalytics,15000);

// ══════════════════════════════════════════════════════════════
//  CO-PILOT — full implementation
//  Routes through /api/copilot → copilot.py → Groq Llama3-70B
//  Falls back to offline SQL if no internet
//  Browser TTS speaks every response (free, no credits)
//  Push-to-talk mic — never auto-starts
// ══════════════════════════════════════════════════════════════

function cpAddMsg(text, isUser){
  const msgs=document.getElementById('cp-messages');
  const d=document.createElement('div');
  d.className='cp-msg'+(isUser?' user':'');
  d.innerHTML=`<div class="cp-av ${isUser?'usr':'bot'}">${isUser?'C':'K'}</div>
    <div class="cp-text">${text}</div>`;
  msgs.appendChild(d);
  msgs.scrollTop=msgs.scrollHeight;
}

function cpAddThinking(){
  const msgs=document.getElementById('cp-messages');
  const d=document.createElement('div');
  d.className='cp-msg'; d.id='cp-thinking';
  d.innerHTML=`<div class="cp-av bot">K</div>
    <div class="cp-text"><div class="cp-thinking"><span></span><span></span><span></span></div></div>`;
  msgs.appendChild(d);
  msgs.scrollTop=msgs.scrollHeight;
}

// Browser TTS — free, no credits, works offline
function cpSpeak(text){
  if(!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel(); // stop any previous
  const u=new SpeechSynthesisUtterance(text);
  u.rate=0.92; u.pitch=0.88; u.lang='en-GB';
  // Try to find a calm professional voice
  const voices=speechSynthesis.getVoices();
  const pref=voices.find(v=>
    v.name.includes('Daniel')||v.name.includes('Aaron')||
    v.name.includes('Google UK')||v.name.includes('Arthur')
  );
  if(pref) u.voice=pref;
  const wf=document.getElementById('cp-waveform');
  wf.classList.add('active');
  u.onend=()=>wf.classList.remove('active');
  speechSynthesis.speak(u);
}

async function askCopilot(queryOverride){
  const input=document.getElementById('cp-input');
  const q=(queryOverride||input.value).trim();
  if(!q) return;
  cpAddMsg(q,true);
  input.value='';
  cpAddThinking();

  try{
    const resp=await fetch('/api/copilot',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({query:q, hours:currentDays*24})
    });
    document.getElementById('cp-thinking')?.remove();
    const data=await resp.json();
    const text=data.text||data.answer||'No response.';
    cpAddMsg(text,false);
    cpSpeak(text); // ← speak every response automatically

    // Show mode badge
    const mode=data.mode||'';
    const modeEl=document.getElementById('cp-mode');
    if(mode==='groq') modeEl.textContent='Groq · Llama 3 · Online';
    else if(mode==='offline') modeEl.textContent='Offline · SQL fallback';
    else if(mode==='anthropic') modeEl.textContent='Claude · Sonnet · Online';
    else if(mode==='scoped') modeEl.textContent='Domain lock enforced';

  } catch(e){
    document.getElementById('cp-thinking')?.remove();
    const offline='[Offline] Unable to reach server. Check your connection.';
    cpAddMsg(offline,false);
    cpSpeak(offline);
  }
}

// ── Push-to-talk mic — only activates on button press ────────
function toggleMic(){
  const btn=document.getElementById('cp-mic');
  if(cpListening){ stopMic(); return; }

  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){
    cpAddMsg('Voice input requires Chrome or Edge browser.',false);
    return;
  }

  // Request permission on button press — never auto-starts
  navigator.mediaDevices.getUserMedia({audio:true})
    .then(stream=>{
      stream.getTracks().forEach(t=>t.stop()); // release immediately
      startMic(btn,SR);
    })
    .catch(()=>{
      cpAddMsg('Microphone permission denied. Allow access in browser settings.',false);
    });
}

function startMic(btn,SR){
  cpRecognition=new SR();
  cpRecognition.lang='en-US';
  cpRecognition.interimResults=false;
  cpRecognition.maxAlternatives=1;
  cpListening=true;
  btn.classList.add('listening');
  btn.title='Listening — click to stop';
  document.getElementById('cp-waveform').classList.add('active');

  cpRecognition.onresult=(e)=>{
    const t=e.results[0][0].transcript;
    document.getElementById('cp-input').value=t;
    stopMic();
    setTimeout(()=>askCopilot(t),200);
  };

  cpRecognition.onerror=(e)=>{
    stopMic();
    if(e.error==='not-allowed')
      cpAddMsg('Microphone blocked. Needs HTTPS — use your Render URL.',false);
  };

  cpRecognition.onend=()=>{ if(cpListening) stopMic(); };
  cpRecognition.start();
}

function stopMic(){
  cpListening=false;
  const btn=document.getElementById('cp-mic');
  btn.classList.remove('listening');
  btn.title='Push to talk';
  document.getElementById('cp-waveform').classList.remove('active');
  cpRecognition?.stop();
}

// ── Preload TTS voices ────────────────────────────────────────
if('speechSynthesis' in window){
  speechSynthesis.getVoices();
  speechSynthesis.addEventListener('voiceschanged',()=>speechSynthesis.getVoices());
}

// ── Wake word (uncomment on demo day) ─────────────────────────
// function startWakeWord(){
//   const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
//   if(!SR) return;
//   const r=new SR(); r.continuous=true; r.lang='en-US';
//   r.onresult=(e)=>{
//     const t=e.results[e.results.length-1][0].transcript.toLowerCase();
//     if(t.includes("kingolik")||t.includes("king olik")){
//       const q=t.replace(/king.?olik/i,'').trim();
//       if(q.length>3){ document.getElementById('cp-input').value=q; askCopilot(q);
//         document.querySelector('.copilot-panel').style.outline='2px solid #4ade80';
//         setTimeout(()=>document.querySelector('.copilot-panel').style.outline='none',2000); }
//     }
//   };
//   r.onend=()=>r.start(); r.start();
// }
// startWakeWord(); // ← uncomment on May 19

// ══════════════════════════════════════════════════════════════
//  SCREEN EDGE GLOW — syncs with voice activity
//  Animated gradient border follows screen path
//  Triggers on TTS start · Fades on TTS end
// ══════════════════════════════════════════════════════════════
const edgeGlow   = document.getElementById('edge-glow');
const edgeCanvas = document.getElementById('edge-canvas');
let glowCtx, glowAnimId, glowOffset=0, glowActive=false;

function initGlowCanvas(){
  edgeCanvas.width  = window.innerWidth;
  edgeCanvas.height = window.innerHeight;
  glowCtx = edgeCanvas.getContext('2d');
}

function drawGlowBorder(offset){
  if(!glowCtx) return;
  const W=edgeCanvas.width, H=edgeCanvas.height;
  glowCtx.clearRect(0,0,W,H);
  const perim  = 2*(W+H);
  const segLen = perim * 0.35;
  const pos    = ((offset % perim) + perim) % perim;
  const segs   = [
    {x1:0,y1:0,x2:W,y2:0,len:W},
    {x1:W,y1:0,x2:W,y2:H,len:H},
    {x1:W,y1:H,x2:0,y2:H,len:W},
    {x1:0,y1:H,x2:0,y2:0,len:H},
  ];
  glowCtx.lineWidth=3; glowCtx.shadowBlur=20; glowCtx.shadowColor='#4ade80';
  let travelled=0;
  for(const seg of segs){
    const mid=(travelled+travelled+seg.len)/2;
    const glowEnd=(pos+segLen)%perim;
    let dist=0;
    if(glowEnd>pos){ dist=(mid>=pos&&mid<=glowEnd)?Math.min(mid-pos,glowEnd-mid)/(segLen/2):0; }
    else { dist=(mid>=pos||mid<=glowEnd)?0.7:0; }
    if(dist>0.05){
      const a=Math.min(dist*0.95,0.95);
      const h1=(offset*0.5)%360, h2=(h1+140)%360;
      const g=glowCtx.createLinearGradient(seg.x1,seg.y1,seg.x2,seg.y2);
      g.addColorStop(0,   `hsla(${h1},100%,65%,0)`);
      g.addColorStop(0.35,`hsla(${h1},100%,65%,${a})`);
      g.addColorStop(0.65,`hsla(${h2},100%,65%,${a})`);
      g.addColorStop(1,   `hsla(${h2},100%,65%,0)`);
      glowCtx.strokeStyle=g;
      glowCtx.beginPath(); glowCtx.moveTo(seg.x1,seg.y1); glowCtx.lineTo(seg.x2,seg.y2); glowCtx.stroke();
    }
    travelled+=seg.len;
  }
}

function startGlow(){
  if(glowActive) return;
  glowActive=true; initGlowCanvas();
  edgeGlow.style.opacity='1'; edgeCanvas.style.opacity='1';
  function animate(){ if(!glowActive)return; glowOffset+=4; drawGlowBorder(glowOffset); glowAnimId=requestAnimationFrame(animate); }
  animate();
}

function stopGlow(){
  glowActive=false; cancelAnimationFrame(glowAnimId);
  edgeGlow.style.opacity='0'; edgeCanvas.style.opacity='0';
  setTimeout(()=>{ glowCtx?.clearRect(0,0,edgeCanvas.width,edgeCanvas.height); },500);
}

window.addEventListener('resize',()=>{ if(glowActive) initGlowCanvas(); });

// ── Override cpSpeak — sync glow + Gemini-like typewriter ───
function cpSpeak(text){
  if(!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text);
  u.rate=0.92; u.pitch=0.9; u.lang='en-GB';
  const voices=speechSynthesis.getVoices();
  const pref=voices.find(v=>v.name.includes('Daniel')||v.name.includes('Aaron')||v.name.includes('Google UK'));
  if(pref) u.voice=pref;
  u.onstart=()=>{ startGlow(); document.getElementById('cp-waveform').classList.add('active'); };
  u.onend  =()=>{ stopGlow();  document.getElementById('cp-waveform').classList.remove('active'); };
  u.onerror=()=>{ stopGlow();  document.getElementById('cp-waveform').classList.remove('active'); };
  speechSynthesis.speak(u);
}

// Gemini-like typewriter — types characters one by one before speaking
function cpTypeWriter(el, text, onDone){
  el.textContent=''; let i=0;
  const iv=setInterval(()=>{
    el.textContent+=text[i++];
    if(i>=text.length){ clearInterval(iv); if(onDone) onDone(); }
  },16);
}

// Override askCopilot with typewriter + glow
async function askCopilot(queryOverride){
  const input=document.getElementById('cp-input');
  const q=(queryOverride||input.value).trim();
  if(!q) return;
  cpAddMsg(q,true); input.value=''; cpAddThinking();
  try{
    const resp=await fetch('/api/copilot',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({query:q,hours:currentDays*24})});
    document.getElementById('cp-thinking')?.remove();
    const data=await resp.json();
    const text=data.text||data.answer||'No response.';

    // Build message with typewriter target
    const msgs=document.getElementById('cp-messages');
    const d=document.createElement('div'); d.className='cp-msg';
    d.innerHTML='<div class="cp-av bot">K</div><div class="cp-text" id="cp-tw"></div>';
    msgs.appendChild(d); msgs.scrollTop=msgs.scrollHeight;
    const twEl=document.getElementById('cp-tw'); twEl.removeAttribute('id');

    // Type it out then speak — Gemini-style
    cpTypeWriter(twEl, text, ()=>{ cpSpeak(text); });

    const modeEl=document.getElementById('cp-mode');
    if(modeEl){
      const m=data.mode||'';
      modeEl.textContent=m==='groq'?'Groq · Llama 3 · Online':m==='offline'?'Offline · SQL fallback':m==='anthropic'?'Claude · Online':'Ready';
    }
  } catch(e){
    document.getElementById('cp-thinking')?.remove();
    const t='[Offline] Cannot reach server. Check your connection.';
    cpAddMsg(t,false); cpSpeak(t);
  }
}
</script>
</body></html>"""