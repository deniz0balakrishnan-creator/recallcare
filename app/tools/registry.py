"""Least-privilege tool registry. An agent can only call tools granted to it; scoping is enforced in code.

The conversation agent's patient-context tool takes NO patient id: it reads ctx.patient_id, so "tell me about
patient X" cannot succeed even if a model tries. Every call (allowed or refused) is traced.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app import clock, db, trace
from app.core import memory, messaging, recall, scheduling
from app.core.lang import fmt_when, visit_label
from app.settings import clinic_config
from app.tools import schemas as S


class ToolPermissionError(Exception):
    pass


@dataclass
class ToolContext:
    run_id: str
    agent: str
    patient_id: int | None = None
    offered_slot_ids: list[str] = field(default_factory=list)
    replacing_appointment_id: int | None = None


@dataclass
class ToolSpec:
    name: str
    schema: type[BaseModel]
    impl: Callable[[Any, ToolContext], Any]
    access: str                      # read | write | send


def _require_own(pid: int, ctx: ToolContext) -> None:
    if ctx.patient_id is None or pid != ctx.patient_id:
        raise ToolPermissionError("tool is scoped to the current patient only")


# ---------------------------------------------------------------- implementations
def _list_patients_due(a: S.ListPatientsDue, ctx: ToolContext) -> list[dict[str, Any]]:
    return [d.to_dict() for d in recall.list_due(a.as_of, a.limit)]


def _get_patient_summary(a: S.GetPatientSummary, ctx: ToolContext) -> dict[str, Any]:
    p = db.q1("SELECT id, preferred_language, birth_date, chas_tier, generation_card, sensitive, whatsapp_consent "
              "FROM patients WHERE id=?", (a.patient_id,))
    if not p:
        raise ToolPermissionError("unknown patient")
    age = recall.age_on(p["birth_date"], clock.today())
    return {"patient_id": p["id"], "language": p["preferred_language"], "age_band": f"{age // 10 * 10}s",
            "chas": p["chas_tier"], "sensitive": bool(p["sensitive"]), "consent": bool(p["whatsapp_consent"])}


def _propose_batch(a: S.ProposeBatch, ctx: ToolContext) -> dict[str, Any]:
    due = {d.patient_id: d for d in recall.list_due(clock.today(), 50)}
    outsiders = [p for p in a.patient_ids if p not in due]
    if outsiders:
        raise ToolPermissionError(f"patients not in the deterministic due list: {outsiders}")
    for pid in a.patient_ids:
        d = due[pid]
        memory.upsert(pid, status="proposed", visit_type=d.visit_type, due_date=d.due_date, score=d.score,
                      urgency=d.urgency, reason=a.reasons[pid][:200], batch_date=clock.today().isoformat(),
                      nudge_count=0, pending_options=None)
        memory.note(pid, f"Proposed for outreach ({d.urgency}).")
    return {"proposed": len(a.patient_ids)}


def _escalate_to_staff(a: S.EscalateToStaff, ctx: ToolContext) -> dict[str, Any]:
    _require_own(a.patient_id, ctx)
    eid = db.execute("INSERT INTO escalations(patient_id,category,urgency,summary_en,status,created_at,run_id) "
                     "VALUES(?,?,?,?, 'open', ?, ?)",
                     (a.patient_id, a.category, a.urgency, a.summary_en, clock.iso(clock.now()), ctx.run_id))
    f = memory.get(a.patient_id)
    if not f or f["status"] != "opted_out":
        memory.transition(a.patient_id, "escalated", force=True)
    memory.note(a.patient_id, f"Escalated to staff: {a.category} ({a.urgency}).")
    return {"escalation_id": eid}


def _record_opt_out(a: S.RecordOptOut, ctx: ToolContext) -> dict[str, Any]:
    _require_own(a.patient_id, ctx)
    db.execute("UPDATE patients SET opted_out=1, opted_out_at=? WHERE id=?", (clock.iso(clock.now()), a.patient_id))
    db.execute("INSERT INTO opt_outs(patient_id,ts,source_text_hash,run_id) VALUES(?,?,?,?)",
               (a.patient_id, clock.iso(clock.now()), a.source_text_hash, ctx.run_id))
    memory.transition(a.patient_id, "opted_out", force=True)
    memory.note(a.patient_id, "Opted out of WhatsApp reminders (permanent).")
    return {"opted_out": True}


def _get_clinic_info(a: S.GetClinicInfo, ctx: ToolContext) -> dict[str, Any]:
    return {"topic": a.topic, "text": clinic_config()["info_sheet"][a.topic]}


def own_context(pid: int) -> dict[str, Any]:
    p = messaging.patient(pid)
    f = memory.get(pid) or {}
    r = db.q1("SELECT visit_type, due_date FROM recalls WHERE patient_id=? AND active=1", (pid,)) or {}
    vt = f.get("visit_type") or r.get("visit_type") or "routine_checkup"
    booking = scheduling.active_booking(pid)
    lang = p["preferred_language"]
    out = {"preferred_name": p["preferred_name"], "language": lang, "visit_type": vt,
           "visit_label": visit_label(vt, "en"), "due_date": f.get("due_date") or r.get("due_date"),
           "status": f.get("status", "due"),
           "booking": ({"appointment_id": booking["id"], "when": fmt_when(clock.parse(booking["start_ts"]), "en"),
                        "dentist": scheduling.dentist_name(booking["dentist_id"])} if booking else None)}
    return out


def _get_own_patient_context(a: S.GetOwnPatientContext, ctx: ToolContext) -> dict[str, Any]:
    if ctx.patient_id is None:
        raise ToolPermissionError("no current patient")
    return own_context(ctx.patient_id)


def _request_scheduling(a: S.RequestScheduling, ctx: ToolContext) -> dict[str, Any]:
    return {"handoff": "scheduling", **a.model_dump()}


def _send_message(a: S.SendMessage, ctx: ToolContext) -> dict[str, Any]:
    _require_own(a.patient_id, ctx)
    return messaging.send_text(a.patient_id, a.text, ctx_patient_id=ctx.patient_id, run_id=ctx.run_id, agent=ctx.agent)


def _find_slots(a: S.FindSlots, ctx: ToolContext) -> dict[str, Any]:
    horizon_end = clock.today() + timedelta(days=clinic_config()["calendar"]["horizon_days"])
    earliest = max(a.earliest, clock.today())
    latest = min(max(a.latest, earliest), horizon_end)
    opts, exact = scheduling.find_slots_with_fallback(earliest, latest, a.part_of_day, a.duration_min, a.weekdays)
    return {"options": opts, "exact_match": exact}


def _book_slot(a: S.BookSlot, ctx: ToolContext) -> dict[str, Any]:
    _require_own(a.patient_id, ctx)
    if a.slot_id not in ctx.offered_slot_ids:
        raise ToolPermissionError("can only book a slot that was offered to this patient")
    return scheduling.book(a.slot_id, a.patient_id, a.visit_type, ctx.run_id,
                           replacing_appointment_id=ctx.replacing_appointment_id)


def _cancel_own_booking(a: S.CancelOwnBooking, ctx: ToolContext) -> dict[str, Any]:
    _require_own(a.patient_id, ctx)
    scheduling.cancel_own(a.appointment_id, a.patient_id)
    return {"cancelled": a.appointment_id}


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("list_patients_due", S.ListPatientsDue, _list_patients_due, "read"),
    ToolSpec("get_patient_summary", S.GetPatientSummary, _get_patient_summary, "read"),
    ToolSpec("propose_batch", S.ProposeBatch, _propose_batch, "write"),
    ToolSpec("escalate_to_staff", S.EscalateToStaff, _escalate_to_staff, "write"),
    ToolSpec("record_opt_out", S.RecordOptOut, _record_opt_out, "write"),
    ToolSpec("get_clinic_info", S.GetClinicInfo, _get_clinic_info, "read"),
    ToolSpec("get_own_patient_context", S.GetOwnPatientContext, _get_own_patient_context, "read"),
    ToolSpec("request_scheduling", S.RequestScheduling, _request_scheduling, "handoff"),
    ToolSpec("send_message", S.SendMessage, _send_message, "send"),
    ToolSpec("find_slots", S.FindSlots, _find_slots, "read"),
    ToolSpec("book_slot", S.BookSlot, _book_slot, "write"),
    ToolSpec("cancel_own_booking", S.CancelOwnBooking, _cancel_own_booking, "write"),
]}

# Least privilege: who may call what. The supervisor routes only; it has no tools.
AGENT_TOOLS: dict[str, tuple[str, ...]] = {
    "supervisor": (),
    "triage": ("list_patients_due", "get_patient_summary", "propose_batch"),
    "guard": ("escalate_to_staff", "record_opt_out"),
    "conversation": ("get_clinic_info", "get_own_patient_context", "request_scheduling", "send_message"),
    "scheduling": ("find_slots", "book_slot", "cancel_own_booking"),
}


def schemas_for(agent: str, names: tuple[str, ...] | None = None) -> dict[str, type[BaseModel]]:
    return {n: TOOLS[n].schema for n in (names or AGENT_TOOLS[agent])}


def call_tool(agent: str, name: str, args: dict[str, Any] | BaseModel, ctx: ToolContext,
              thought_summary: str | None = None, llm=None) -> Any:
    """Permission check → schema validation → scoped implementation → trace. Raises on any refusal."""
    if name not in AGENT_TOOLS.get(agent, ()):
        trace.event(ctx.run_id, agent, f"tool:{name}", outcome="blocked", patient_id=ctx.patient_id,
                    args={"reason": "tool not granted to this agent"}, llm=llm)
        raise ToolPermissionError(f"{agent} may not call {name}")
    spec = TOOLS[name]
    raw = args.model_dump(mode="json") if isinstance(args, BaseModel) else args
    try:
        parsed = spec.schema.model_validate(raw)
    except ValidationError as e:
        trace.event(ctx.run_id, agent, f"tool:{name}", outcome="blocked", patient_id=ctx.patient_id,
                    args={"reason": "schema validation failed", "errors": str(e)[:200]}, llm=llm)
        raise ToolPermissionError(f"invalid args for {name}") from e
    try:
        result = spec.impl(parsed, ctx)
    except (ToolPermissionError, scheduling.BookingError) as e:
        trace.event(ctx.run_id, agent, f"tool:{name}", outcome="blocked", patient_id=ctx.patient_id,
                    args={**raw, "refused": str(e)}, thought_summary=thought_summary, llm=llm)
        raise
    trace.event(ctx.run_id, agent, f"tool:{name}", outcome="ok", patient_id=ctx.patient_id, args=raw,
                thought_summary=thought_summary, llm=llm)
    return result


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
