# ════════════════════════════════════════════════════════
# PASTE THIS INTO app.py TEMPORARILY to diagnose AT routing
# Replace your /voice/answer route with this minimal version
# If you hear "System working" when your phone is called,
# the problem was your greeting XML. If you still get hung up,
# the problem is AT config.
# ════════════════════════════════════════════════════════

@app.route("/voice/answer", methods=["POST", "GET"])
def voice_answer():
    # Log EVERYTHING AT sends — this is your diagnostic tool
    print("=" * 60)
    print("FIRE: /voice/answer HIT")
    print(f"  Method:  {request.method}")
    print(f"  Form:    {dict(request.form)}")
    print(f"  Args:    {dict(request.args)}")
    print(f"  Values:  {dict(request.values)}")
    print("=" * 60)

    caller      = (request.values.get("callerNumber")      or "").strip()
    destination = (request.values.get("destinationNumber") or "").strip()
    direction   = (request.values.get("direction")         or "").strip().lower()
    call_state  = (request.values.get("callSessionState")  or "").strip().lower()
    session_id  = (request.args.get("session_id")          or
                   request.values.get("sessionId")         or "").strip()

    print(f"  caller={caller}  dest={destination}  dir={direction}  state={call_state}")

    # Determine if outbound (we called them) or inbound (they called us)
    is_outbound = False
    if direction == "outbound":
        is_outbound = True
    elif direction == "inbound":
        is_outbound = False
    else:
        # No direction — check _pending
        user_phone = destination if destination != YOUR_NUMBER else caller
        is_outbound = (user_phone in _pending or caller in _pending)
        print(f"  direction missing — inferred {'outbound' if is_outbound else 'inbound'}")

    if not is_outbound:
        # Inbound flash — reject + callback
        flash_caller = caller
        if not flash_caller:
            return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>')

        if ALLOWED and flash_caller not in ALLOWED:
            return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>')

        if _limited(flash_caller):
            return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>')

        new_sid = f"flash_{datetime.datetime.utcnow().strftime('%H%M%S%f')[:15]}"
        from database import save_session
        save_session({
            "session_id": new_sid, "phone": flash_caller,
            "menu_choice": "flash",
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "status": "pending_call"
        })
        print(f"  Flash from {flash_caller} — reject + callback in 5s")
        threading.Thread(target=_call_back, args=(flash_caller, new_sid), daemon=True).start()
        return _xml('<?xml version="1.0" encoding="UTF-8"?><Response><Reject/></Response>')

    # Outbound — resolve session
    if not session_id:
        user_phone = destination if destination != YOUR_NUMBER else caller
        session_id = (
            _pending.pop(user_phone, None) or
            _pending.pop(caller, None) or
            _pending.pop(destination, None) or
            f"ans_{datetime.datetime.utcnow().strftime('%H%M%S')}"
        )
        print(f"  Resolved session from _pending: {session_id[-8:]}")

    print(f"  Outbound ANSWERED — serving greeting for [{session_id[-8:]}]")

    # MINIMAL XML — no complex logic, fastest possible response
    # If user hears this, your system works end-to-end
    cb = f"{BASE_URL}/voice/save?session_id={session_id}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="woman" playBeep="false">Habari. Karibu King olik. Sema ujumbe wako baada ya mlio. Bonyeza nyota ukimaliza.</Say>
  <Record finishOnKey="*" maxLength="120" trimSilence="true" playBeep="true" callbackUrl="{cb}"/>
</Response>"""

    print(f"  Serving XML with callback: {cb}")
    return xml, 200, {"Content-Type": "application/xml"}