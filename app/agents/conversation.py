"""Conversation agent: outreach templates, logistics answers from the info sheet, hand-offs to scheduling,
and deterministic rendering of scheduling results (the model never writes times, prices or safety texts)."""
from __future__ import annotations

from typing import Any

from app import clock, db, trace
from app.agents.common import llm_step, prompt, wrap_untrusted
from app.core import memory, messaging
from app.core.lang import LANG_NAMES, fmt_when, render, render_pair, visit_label
from app.core.memory import TransitionError
from app.llm.protocol import tool_block
from app.settings import clinic_config
from app.tools import schemas as S
from app.tools.registry import ToolContext, ToolPermissionError, call_tool, own_context

MAX_INNER_STEPS = 3


def _send(state: dict[str, Any], text: str, gloss: str | None) -> dict[str, Any]:
    pid = state["patient_id"]
    ctx = ToolContext(run_id=state["run_id"], agent="conversation", patient_id=pid)
    res = call_tool("conversation", "send_message", {"patient_id": pid, "text": text}, ctx)
    if gloss is not None and res.get("message_id"):
        db.execute("UPDATE messages SET gloss_en=? WHERE id=?", (gloss[:1000], res["message_id"]))
    return res


def _set_status(pid: int, status: str) -> None:
    try:
        memory.transition(pid, status)
    except TransitionError:
        memory.transition(pid, status, force=True)


# ---------------------------------------------------------------- outreach (templates, no LLM)
def _outreach(state: dict[str, Any]) -> dict[str, Any]:
    pid, run_id = state["patient_id"], state["run_id"]
    f = memory.get(pid) or {}
    renudge = state["event_type"] == "renudge"
    res = messaging.send_template(pid, ctx_patient_id=pid, visit_type=f.get("visit_type") or "routine_checkup",
                                  run_id=run_id, agent="conversation", reason="re-nudge" if renudge else "recall")
    upd: dict[str, Any] = {"visited": state.get("visited", []) + ["conversation"], "outcome": f"template:{res['status']}"}
    if res["status"] == "sent":
        if renudge:
            memory.upsert(pid, nudge_count=(f.get("nudge_count") or 0) + 1)
            memory.note(pid, "Gentle re-nudge sent (template).")
        else:
            _set_status(pid, "contacted")
            memory.note(pid, f"Recall template sent ({state.get('language')}).")
        upd["messages"] = [{"role": "clinic", "text": "[recall reminder template]"}]
    return upd


# ---------------------------------------------------------------- scheduling results → deterministic text
def _options_text(options: list[dict[str, Any]], lang: str) -> str:
    return "\n".join(f"{i}) {fmt_when(clock.parse(o['start_ts']), lang)} · {o['dentist']}" for i, o in enumerate(options, 1))


def _render_scheduling(state: dict[str, Any]) -> dict[str, Any]:
    pid, lang = state["patient_id"], state.get("language", "en")
    r = state["scheduling_result"]
    kind = r["type"]
    clinic = clinic_config()["clinic"]
    addr = clinic["address"].split(", Singapore")[0]
    upd: dict[str, Any] = {"scheduling_rendered": True, "visited": state.get("visited", []) + ["conversation"]}
    kw: dict[str, str] = {}
    if kind in ("offer", "slot_taken", "options_expired"):
        key = kind if kind in ("slot_taken", "options_expired") else ("slot_offer" if r.get("exact") else "slot_offer_nearest")
        kw = {"options": _options_text(r["options"], lang)}
        text = render(key, lang, **kw)
        gloss = render(key, "en", options=_options_text(r["options"], "en"))
        memory.upsert(pid, pending_options=r["options"])
    elif kind in ("booked", "rescheduled"):
        b = r["booking"]
        when = clock.parse(b["start_ts"])
        key = "booking_confirm" if kind == "booked" else "reschedule_confirm"
        text = render(key, lang, when=fmt_when(when, lang), dentist=b["dentist"], address=addr)
        gloss = render(key, "en", when=fmt_when(when, "en"), dentist=b["dentist"], address=addr)
        _set_status(pid, "booked")
        memory.upsert(pid, pending_options=None)
        memory.note(pid, f"{'Booked' if kind == 'booked' else 'Rescheduled'}: {fmt_when(when, 'en')} with {b['dentist']}.")
        upd["booking"] = b
    elif kind == "cancelled":
        when = clock.parse(r["start_ts"])
        text, gloss = render("cancel_confirm", lang, when=fmt_when(when, lang)), render("cancel_confirm", "en", when=fmt_when(when, "en"))
        _set_status(pid, "declined")
        memory.note(pid, "Patient cancelled their booking.")
        upd["booking"] = None
    elif kind == "already_booked":
        b = r["booking"]
        text = render("already_booked", lang, when=b["when_local"], dentist=b["dentist"])
        gloss = render("already_booked", "en", when=b["when"], dentist=b["dentist"])
    else:   # invalid_option | booking_limit | no_slots | no_booking
        key = {"invalid_option": "invalid_option", "booking_limit": "booking_limit", "no_slots": "no_slots"}.get(kind, "ask_when")
        visit = visit_label(own_context(pid)["visit_type"], lang)
        text, gloss = render(key, lang, visit=visit), render(key, "en", visit=visit_label(own_context(pid)["visit_type"], "en"))
        if kind == "no_slots":
            upd["escalation_request"] = {"category": "scheduling", "urgency": "routine",
                                         "summary_en": "No free slots in the next 14 days; please call the patient to arrange a time."}
    res = _send(state, text, gloss)
    upd["outcome"] = f"{kind}:{res['status']}"
    upd["messages"] = [{"role": "clinic", "text": text[:400]}]
    return upd


# ---------------------------------------------------------------- inbound conversation
def _fast_path(state: dict[str, Any], ctx_info: dict[str, Any]) -> dict[str, Any] | None:
    """Unambiguous short replies need no model call (cheaper, faster, deterministic)."""
    short = (state.get("guard_verdict") or {}).get("short_reply")
    if not short:
        return None
    pending = state.get("proposed_slots") or []
    has_booking = bool(ctx_info["booking"])
    lang = state.get("language", "en")
    visit = visit_label(ctx_info["visit_type"], lang)
    visit_en = visit_label(ctx_info["visit_type"], "en")
    sched = lambda intent, n=None: {"pending_scheduling": {"intent": intent, "preference_text": "", "option_number": n}}
    if short.startswith("option:"):
        return sched("choose_option", int(short.split(":")[1])) if pending else {"reply": ("invalid_option" if has_booking else "clarify", visit, visit_en)}
    if short == "yes":
        return sched("find_times") if not has_booking else {"reply": ("thanks_ack", visit, visit_en)}
    if short == "no":
        return {"reply": ("decline_ack", visit, visit_en), "status": "declined"} if not has_booking else {"reply": ("thanks_ack", visit, visit_en)}
    if short == "thanks":
        return {"reply": ("thanks_ack", visit, visit_en)}
    if short == "change":
        return sched("reschedule" if has_booking else "find_times")
    if short == "cancel":
        return sched("cancel") if has_booking else {"reply": ("ask_when", visit, visit_en)}
    if short == "greeting":
        return {"reply": ("ask_when", visit, visit_en)}
    return None


def _context_message(state: dict[str, Any], ci: dict[str, Any], tool_results: list[dict[str, Any]], text: str) -> str:
    pending = state.get("proposed_slots") or []
    opts = "; ".join(f"{i}) {fmt_when(clock.parse(o['start_ts']), 'en')} {o['dentist']}" for i, o in enumerate(pending, 1)) or "none"
    booking = f"{ci['booking']['when']} with {ci['booking']['dentist']}" if ci["booking"] else "none"
    history = "\n".join(f"{m['role']}: {m['text'][:200]}" for m in (state.get("messages") or [])[:-1][-6:]) or "(none)"
    results = "\n".join(f"- {r['tool']}({r['args']}) = {str(r['result'])[:700]}" for r in tool_results) or "(none yet)"
    return (f"PATIENT: {ci['preferred_name']}; replies in {LANG_NAMES[ci['language']]}; due: {ci['visit_label']} "
            f"(due {ci['due_date']}); status: {ci['status']}; current booking: {booking}\n"
            f"OPTIONS ALREADY OFFERED: {opts}\nRECENT CONVERSATION (oldest first):\n{history}\n"
            f"TOOL RESULTS:\n{results}\n{wrap_untrusted(text)}")


def _converse(state: dict[str, Any]) -> dict[str, Any]:
    pid, run_id, lang = state["patient_id"], state["run_id"], state.get("language", "en")
    ci = own_context(pid)
    visited = state.get("visited", []) + ["conversation"]
    fp = _fast_path(state, ci)
    if fp is not None:
        trace.event(run_id, "conversation", "fast_path", patient_id=pid,
                    args={"short_reply": state["guard_verdict"]["short_reply"]},
                    thought_summary="unambiguous short reply handled without a model call")
        if "pending_scheduling" in fp:
            return {**fp, "visited": visited}
        key, visit, visit_en = fp["reply"]
        text, gloss = render(key, lang, visit=visit), render(key, "en", visit=visit_en)
        res = _send(state, text, gloss)
        if fp.get("status"):
            _set_status(pid, fp["status"])
            memory.note(pid, "Patient declined for now.")
        return {"visited": visited, "outcome": f"{key}:{res['status']}", "messages": [{"role": "clinic", "text": text}]}

    text = state.get("inbound_text") or ""
    allowed = {"get_clinic_info": S.GetClinicInfo, "get_own_patient_context": S.GetOwnPatientContext,
               "request_scheduling": S.RequestScheduling, "respond": S.Respond, "escalate": S.Escalate}
    tools = tool_block({k: v for k, v in allowed.items() if k not in ("respond", "escalate")})
    system = prompt("conversation", clinic=clinic_config()["clinic"]["name"], visit=ci["visit_label"],
                    clinic_kind=clinic_config()["clinic"].get("kind", "clinic"),
                    now=clock.now().strftime("%a %d %b %Y %H:%M"), lang_name=LANG_NAMES[lang], tools=tools)
    tool_results: list[dict[str, Any]] = []
    tctx = ToolContext(run_id=run_id, agent="conversation", patient_id=pid)
    for _ in range(MAX_INNER_STEPS):
        out = llm_step("conversation", run_id=run_id, patient_id=pid, system=system,
                       user=_context_message(state, ci, tool_results, text), allowed=allowed,
                       context={"text": text, "lang": lang, "visit_label": visit_label(ci["visit_type"], lang),
                                "tool_results": tool_results, "short_reply": None,
                                "pending_options": state.get("proposed_slots") or [], "has_booking": bool(ci["booking"])},
                       max_tokens=500)
        if not out.ok:
            unavailable = (out.error or "").startswith("llm unavailable")
            return {"visited": visited, "escalation_request": {
                "category": "ai_unavailable" if unavailable else "unparseable_output", "urgency": "routine",
                "summary_en": (f"AI assistant unavailable ({out.error}); please reply to the patient yourself. " if unavailable else
                               f"Conversation agent could not produce a valid action ({out.error}). ")
                              + f"Patient wrote: \"{text[:200]}\""}}
        act, args = out.action, out.args
        if act.action in ("get_clinic_info", "get_own_patient_context"):
            result = call_tool("conversation", act.action, args, tctx, thought_summary=act.thought_summary)
            tool_results.append({"tool": act.action, "args": args.model_dump(), "result": result})
            continue
        if act.action == "request_scheduling":
            call_tool("conversation", "request_scheduling", args, tctx, thought_summary=act.thought_summary)
            return {"visited": visited, "pending_scheduling": args.model_dump()}
        if act.action == "escalate":
            return {"visited": visited, "escalation_request": args.model_dump()}
        # respond
        res = _send(state, args.text, args.gloss_en or None)
        if res["status"] == "blocked":
            safe, gloss = render_pair("fallback_safe", lang)
            _send(state, safe, gloss)
            return {"visited": visited, "outcome": "reply_blocked",
                    "escalation_request": {"category": "low_confidence", "urgency": "routine",
                                           "summary_en": f"Model reply blocked by output validator ({'; '.join(res.get('reasons', []))}). "
                                                         f"Patient wrote: \"{text[:200]}\""}}
        if args.set_status == "declined":
            _set_status(pid, "declined")
            memory.note(pid, "Patient declined for now.")
        for r in tool_results:
            if r["tool"] == "get_clinic_info":
                memory.note(pid, f"Answered logistics question: {r['args']['topic']}.")
        return {"visited": visited, "outcome": f"respond:{res['status']}", "messages": [{"role": "clinic", "text": args.text[:400]}]}
    return {"visited": visited, "escalation_request": {"category": "loop_limit", "urgency": "routine",
            "summary_en": f"Conversation agent hit its {MAX_INNER_STEPS}-step tool limit. Patient wrote: \"{text[:200]}\""}}


def conversation_node(state: dict[str, Any]) -> dict[str, Any]:
    if state["event_type"] in ("outreach", "renudge"):
        return _outreach(state)
    if state.get("scheduling_result") and not state.get("scheduling_rendered"):
        return _render_scheduling(state)
    try:
        return _converse(state)
    except ToolPermissionError as e:
        return {"visited": state.get("visited", []) + ["conversation"],
                "escalation_request": {"category": "security", "urgency": "soon",
                                       "summary_en": f"Blocked tool call by conversation agent: {e}"}}
