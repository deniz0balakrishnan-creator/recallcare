"""Entry points used by the web app, the webhook, the scheduler and the eval harness."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
from typing import Any

from app import clock, db, trace
from app.core import memory, messaging
from app.core.lang import visit_label
from app.core.memory import TransitionError
from app.graph import RUN_LOCK, get_graph
from app.settings import clinic_config, settings

PER_RUN_RESET = {
    "inbound_text": None, "inbound_message_id": None, "guard_verdict": None, "pending_scheduling": None,
    "scheduling_result": None, "scheduling_rendered": False, "escalation_request": None, "escalation": None,
    "batch": None, "route": None, "visited": [], "step_count": 0, "outcome": None,
}


def _invoke(event_type: str, patient_id: int | None, source: str = "app", **inputs: Any) -> dict[str, Any]:
    run_id = trace.new_run(event_type, patient_id, source)
    thread = f"patient-{patient_id}" if patient_id else f"triage-{clock.today().isoformat()}"
    init: dict[str, Any] = {**PER_RUN_RESET, "event_type": event_type, "run_id": run_id, "thread_id": thread,
                            "patient_id": patient_id, **inputs}
    if patient_id:
        p = messaging.patient(patient_id)
        f = memory.get(patient_id) or {}
        init["language"] = p["preferred_language"]
        init["visit_label_local"] = visit_label(f.get("visit_type") or "routine_checkup", p["preferred_language"])
    try:
        with RUN_LOCK:
            final = get_graph().invoke(init, config={"configurable": {"thread_id": thread}, "recursion_limit": 40})
    except Exception as e:                                   # never crash the webhook; record and surface
        trace.event(run_id, "supervisor", "run_error", outcome="error", patient_id=patient_id,
                    args={"error": f"{type(e).__name__}: {str(e)[:200]}"})
        trace.end_run(run_id, "error")
        raise
    trace.end_run(run_id, final.get("outcome") or "ok", final.get("step_count") or 0)
    final["run_id"] = run_id
    return final


# ---------------------------------------------------------------- daily triage + staff approval (Tier 2)
def run_daily_triage(source: str = "app") -> dict[str, Any]:
    return _invoke("daily_triage", None, source)


def proposed_batch() -> list[dict[str, Any]]:
    return db.q("""SELECT f.*, p.full_name, p.preferred_name, p.preferred_language, p.sensitive, p.birth_date,
                          p.chas_tier, p.generation_card
                   FROM followups f JOIN patients p ON p.id=f.patient_id
                   WHERE f.status='proposed' ORDER BY f.score DESC""")


def approve(patient_ids: list[int], staff: str = "staff", source: str = "app") -> list[dict[str, Any]]:
    """Staff approve (Tier 2). Each approved patient gets the recall template via the conversation agent."""
    results = []
    for pid in patient_ids:
        f = memory.get(pid)
        if not f or f["status"] != "proposed":
            results.append({"patient_id": pid, "status": "skipped (not proposed)"})
            continue
        run = trace.new_run("staff_action", pid, source)
        memory.transition(pid, "approved")
        memory.note(pid, f"Approved for outreach by {staff}.")
        trace.event(run, "staff", "approve_outreach", patient_id=pid, args={"by": staff, "tier": 2})
        trace.end_run(run, "approved")
        out = _invoke("outreach", pid, source)
        results.append({"patient_id": pid, "status": out.get("outcome"), "run_id": out["run_id"]})
    return results


def remove_from_batch(pid: int, staff: str = "staff") -> None:
    run = trace.new_run("staff_action", pid)
    memory.transition(pid, "due", force=True, batch_date=None)
    memory.note(pid, f"Removed from today's batch by {staff}.")
    trace.event(run, "staff", "remove_from_batch", patient_id=pid, args={"by": staff})
    trace.end_run(run, "removed")


def edit_reason(pid: int, reason: str, staff: str = "staff") -> None:
    memory.upsert(pid, reason=reason[:200])
    run = trace.new_run("staff_action", pid)
    trace.event(run, "staff", "edit_reason", patient_id=pid, args={"by": staff, "reason": reason[:200]})
    trace.end_run(run, "edited")


# ---------------------------------------------------------------- inbound messages
_rate: dict[str, deque] = defaultdict(deque)


def rate_limited(phone: str) -> bool:
    now = clock.now().timestamp()
    q = _rate[phone]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= settings.inbound_rate_per_min:
        return True
    q.append(now)
    return False


def handle_inbound(*, patient_id: int | None = None, from_phone: str | None = None, text: str,
                   channel: str | None = None, wa_message_id: str | None = None, source: str = "app") -> dict[str, Any]:
    channel = channel or messaging.current_channel()
    p = messaging.patient(patient_id) if patient_id else messaging.find_patient_by_phone(from_phone or "")
    if not p:
        return {"status": "unknown_sender"}           # never reply to unknown numbers
    pid = p["id"]
    if wa_message_id and db.q1("SELECT id FROM messages WHERE wa_message_id=?", (wa_message_id,)):
        return {"status": "duplicate"}                # Meta retries webhooks; process each message once
    if rate_limited(from_phone or p["phone"]):
        run = trace.new_run("inbound_message", pid, source)
        trace.event(run, "supervisor", "rate_limited", outcome="blocked", patient_id=pid,
                    args={"limit_per_min": settings.inbound_rate_per_min})
        trace.end_run(run, "rate_limited")
        return {"status": "rate_limited"}
    mid = messaging.store_inbound(pid, text, channel, wa_message_id, None)
    f = memory.get(pid)
    if not f:
        memory.upsert(pid, status="replied")
    elif f["status"] in ("contacted", "no_response", "declined", "due", "proposed", "approved"):
        try:
            memory.transition(pid, "replied", force=f["status"] in ("due", "proposed", "approved"))
        except TransitionError:
            pass
    if p["opted_out"]:
        run = trace.new_run("inbound_message", pid, source)
        trace.event(run, "supervisor", "opted_out_patient", outcome="skipped", patient_id=pid,
                    args={"note": "stored for staff; no automated reply to an opted-out patient"})
        trace.end_run(run, "opted_out")
        return {"status": "opted_out"}
    out = _invoke("inbound_message", pid, source, inbound_text=text, inbound_message_id=mid)
    db.execute("UPDATE messages SET run_id=? WHERE id=?", (out["run_id"], mid))
    return {"status": "processed", "run_id": out["run_id"], "outcome": out.get("outcome"),
            "verdict": (out.get("guard_verdict") or {}).get("category")}


# ---------------------------------------------------------------- follow-through
def run_renudges(source: str = "app") -> dict[str, int]:
    """One gentle re-nudge after N days of silence; after that, mark no_response. Never nags opted-out patients."""
    cfg = clinic_config()["outreach"]
    now = clock.now()
    sent = closed = 0
    for f in db.q("SELECT f.*, p.sensitive FROM followups f JOIN patients p ON p.id=f.patient_id "
                  "WHERE f.status='contacted' AND p.opted_out=0"):
        if f["sensitive"]:
            continue                  # Tier 2: staff decide any further contact with a sensitive patient
        last = clock.parse(f["last_contact_at"])
        if not last:
            continue
        if (f["nudge_count"] or 0) < cfg["max_nudges"] and now - last >= timedelta(days=cfg["renudge_after_days"]):
            _invoke("renudge", f["patient_id"], source)
            sent += 1
        elif (f["nudge_count"] or 0) >= cfg["max_nudges"] and now - last >= timedelta(
                days=cfg["no_response_after_days"] - cfg["renudge_after_days"]):
            memory.transition(f["patient_id"], "no_response")
            memory.note(f["patient_id"], "No response after reminder + one re-nudge; closed for this cycle.")
            closed += 1
    return {"renudged": sent, "closed_no_response": closed}


def resolve_escalation(eid: int, resolution: str, staff: str = "staff") -> None:
    e = db.q1("SELECT * FROM escalations WHERE id=?", (eid,))
    if not e:
        return
    db.execute("UPDATE escalations SET status='resolved', resolved_at=?, resolution=? WHERE id=?",
               (clock.iso(clock.now()), resolution[:300], eid))
    run = trace.new_run("staff_action", e["patient_id"])
    trace.event(run, "staff", "resolve_escalation", patient_id=e["patient_id"], args={"by": staff, "resolution": resolution[:200]})
    trace.end_run(run, "resolved")
    memory.note(e["patient_id"], f"Escalation resolved by staff: {resolution[:80]}")


def staff_reply(pid: int, text: str, staff: str = "staff") -> dict[str, Any]:
    run = trace.new_run("staff_action", pid)
    res = messaging.send_text(pid, text, ctx_patient_id=pid, run_id=run, agent=f"staff:{staff}", kind="staff",
                              bypass_sensitive=True)
    trace.end_run(run, res["status"])
    return res
