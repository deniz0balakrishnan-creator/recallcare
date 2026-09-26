"""The ONLY path to a patient. Order: validator → consent/opt-out → Tier-2 hold → 24h window → adapter (allowlist)."""
from __future__ import annotations

import logging
from typing import Any

from app import clock, db, trace
from app.channels.adapters import get_adapter
from app.channels.base import RecipientNotAllowed, WindowClosed
from app.core import memory
from app.core.lang import visit_label
from app.core.validator import effective_phone, validate_outbound
from app.settings import clinic_config, settings, templates_config

log = logging.getLogger("recallcare.messaging")


def current_channel() -> str:
    return db.get_setting("channel") or settings.channel


def set_channel(name: str) -> None:
    if name not in {"simulator", "whatsapp"}:
        raise ValueError("unknown channel")
    db.set_setting("channel", name)


def patient(pid: int) -> dict[str, Any]:
    p = db.q1("SELECT * FROM patients WHERE id=?", (pid,))
    if not p:
        raise KeyError(f"patient {pid} not found")
    return p


def _store(pid: int, *, direction: str, channel: str, kind: str, body: str, lang: str | None, gloss_en: str | None,
           status: str, run_id: str | None, agent: str | None, template_name: str | None = None,
           wa_message_id: str | None = None) -> int:
    return db.execute(
        "INSERT INTO messages(patient_id,direction,channel,kind,template_name,lang,body,gloss_en,status,wa_message_id,ts,run_id,agent)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, direction, channel, kind, template_name, lang, body, gloss_en, status, wa_message_id,
         clock.iso(clock.now()), run_id, agent))


def template_preview(template_name: str, lang: str, params: list[str]) -> str:
    tpl = templates_config()["templates"][template_name]["languages"]
    body = (tpl.get(lang) or tpl["en"])["body"]
    for i, p in enumerate(params, start=1):
        body = body.replace("{{%d}}" % i, p)
    return body


def recall_template_params(p: dict[str, Any], visit_type: str) -> list[str]:
    return [p["preferred_name"], clinic_config()["clinic"]["name"], visit_label(visit_type, p["preferred_language"])]


def send_template(pid: int, *, ctx_patient_id: int, visit_type: str, run_id: str, agent: str,
                  reason: str = "recall") -> dict[str, Any]:
    p = patient(pid)
    lang = p["preferred_language"]
    channel = current_channel()
    name = settings.wa_template_name
    if channel == "whatsapp" and not settings.wa_template_approved:
        name, lang_used, params = "hello_world", "en", []          # Meta's pre-approved stand-in
    else:
        lang_used, params = lang, recall_template_params(p, visit_type)
    body = template_preview(name, lang_used, params)
    gloss = template_preview(name, "en", recall_template_params(p, visit_type) if params else [])
    chk = validate_outbound(patient=p, ctx_patient_id=ctx_patient_id, text=body, lang=lang_used, channel=channel,
                            kind="template")
    if not chk.ok:
        mid = _store(pid, direction="out", channel=channel, kind="template", body=body, lang=lang_used, gloss_en=gloss,
                     status="blocked", run_id=run_id, agent=agent, template_name=name)
        trace.event(run_id, agent, "send_template", outcome="blocked", patient_id=pid,
                    args={"template": name, "reasons": chk.reasons})
        return {"status": "blocked", "reasons": chk.reasons, "message_id": mid}
    try:
        res = get_adapter(channel).send_template(effective_phone(p), name, lang_used, params)
    except RecipientNotAllowed as e:
        trace.event(run_id, agent, "send_template", outcome="blocked", patient_id=pid, args={"error": str(e)})
        return {"status": "blocked", "reasons": [str(e)]}
    status = "sent" if res.ok else "failed"
    mid = _store(pid, direction="out", channel=channel, kind="template", body=body, lang=lang_used, gloss_en=gloss,
                 status=status, run_id=run_id, agent=agent, template_name=name, wa_message_id=res.provider_id)
    trace.event(run_id, agent, "send_template", outcome="ok" if res.ok else "error", patient_id=pid,
                args={"template": name, "lang": lang_used, "reason": reason, "error": res.error})
    if res.ok:
        memory.upsert(pid, last_contact_at=clock.iso(clock.now()))
    return {"status": status, "message_id": mid, "error": res.error}


def send_text(pid: int, text: str, *, ctx_patient_id: int, run_id: str, agent: str, gloss_en: str | None = None,
              allow_opted_out: bool = False, bypass_sensitive: bool = False, kind: str = "text") -> dict[str, Any]:
    """Free-text send inside the 24h window. Returns status: sent|blocked|pending_approval|window_closed|failed."""
    p = patient(pid)
    lang = p["preferred_language"]
    channel = current_channel()
    chk = validate_outbound(patient=p, ctx_patient_id=ctx_patient_id, text=text, lang=lang, channel=channel,
                            kind="text" if kind != "staff" else "staff", allow_opted_out=allow_opted_out)
    if not chk.ok:
        mid = _store(pid, direction="out", channel=channel, kind=kind, body=text, lang=lang, gloss_en=gloss_en,
                     status="blocked", run_id=run_id, agent=agent)
        trace.event(run_id, "validator", "block_outbound", outcome="blocked", patient_id=pid,
                    args={"reasons": chk.reasons, "text": text})
        return {"status": "blocked", "reasons": chk.reasons, "message_id": mid}
    if p["sensitive"] and not bypass_sensitive:
        mid = _store(pid, direction="out", channel=channel, kind=kind, body=text, lang=lang, gloss_en=gloss_en,
                     status="pending_approval", run_id=run_id, agent=agent)
        trace.event(run_id, "supervisor", "hold_for_staff_approval", outcome="ok", patient_id=pid,
                    args={"tier": 2, "reason": "patient flagged sensitive", "text": text})
        return {"status": "pending_approval", "message_id": mid}
    window = memory.window_open(pid)
    try:
        res = get_adapter(channel).send_text(effective_phone(p), text, window_open=window)
    except WindowClosed as e:
        trace.event(run_id, agent, "send_message", outcome="blocked", patient_id=pid,
                    args={"error": str(e), "fallback": "template required"})
        return {"status": "window_closed", "reasons": [str(e)]}
    except RecipientNotAllowed as e:
        trace.event(run_id, agent, "send_message", outcome="blocked", patient_id=pid, args={"error": str(e)})
        return {"status": "blocked", "reasons": [str(e)]}
    status = "sent" if res.ok else "failed"
    mid = _store(pid, direction="out", channel=channel, kind=kind, body=text, lang=lang, gloss_en=gloss_en, status=status,
                 run_id=run_id, agent=agent, wa_message_id=res.provider_id)
    trace.event(run_id, agent, "send_message", outcome="ok" if res.ok else "error", patient_id=pid,
                args={"text": text, "channel": channel, "error": res.error})
    if res.ok:
        memory.upsert(pid, last_contact_at=clock.iso(clock.now()))
    return {"status": status, "message_id": mid, "error": res.error}


def release_pending(message_id: int, *, approve: bool, staff: str) -> dict[str, Any]:
    """Tier 2: staff approves or rejects a held message."""
    m = db.q1("SELECT * FROM messages WHERE id=? AND status='pending_approval'", (message_id,))
    if not m:
        return {"status": "not_found"}
    run_id = trace.new_run("staff_action", m["patient_id"])
    if not approve:
        db.execute("UPDATE messages SET status='rejected' WHERE id=?", (message_id,))
        trace.event(run_id, "staff", "reject_message", patient_id=m["patient_id"], args={"message_id": message_id, "by": staff})
        trace.end_run(run_id, "rejected")
        return {"status": "rejected"}
    db.execute("UPDATE messages SET status='approved_released' WHERE id=?", (message_id,))
    trace.event(run_id, "staff", "approve_message", patient_id=m["patient_id"], args={"message_id": message_id, "by": staff})
    out = send_text(m["patient_id"], m["body"], ctx_patient_id=m["patient_id"], run_id=run_id, agent=m["agent"] or "conversation",
                    gloss_en=m["gloss_en"], bypass_sensitive=True)
    if out["status"] == "window_closed":
        # The 24h window closed while the message waited for approval: WhatsApp rules require a template now.
        db.execute("UPDATE messages SET status='expired' WHERE id=?", (message_id,))
        f = memory.get(m["patient_id"]) or {}
        out = send_template(m["patient_id"], ctx_patient_id=m["patient_id"], visit_type=f.get("visit_type") or "routine_checkup",
                            run_id=run_id, agent="conversation", reason="window closed while awaiting approval")
        out["fallback"] = "template"
    trace.end_run(run_id, out["status"])
    return out


def store_inbound(pid: int, text: str, channel: str, wa_message_id: str | None, run_id: str | None) -> int:
    mid = _store(pid, direction="in", channel=channel, kind="text", body=text, lang=None, gloss_en=None,
                 status="received", run_id=run_id, agent=None, wa_message_id=wa_message_id)
    memory.open_window(pid)
    return mid


def find_patient_by_phone(phone: str) -> dict[str, Any] | None:
    for pid, ph in settings.phone_overrides.items():
        if ph == phone:
            return db.q1("SELECT * FROM patients WHERE id=?", (pid,))
    return db.q1("SELECT * FROM patients WHERE phone=?", (phone,))
