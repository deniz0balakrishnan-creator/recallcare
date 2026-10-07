"""Safety guard: screens EVERY inbound message before any other agent sees it. Nothing overrides it.

Order: deterministic pre-screen (4 languages) → LLM classification (only when needed) → combine, where the
model can raise severity but never clear a deterministic hit → act (escalate / opt-out / clarify / allow).
It also executes escalations requested by other agents or by the supervisor (loop limit), since it owns the
escalate_to_staff tool.
"""
from __future__ import annotations

from typing import Any

from app import db, trace
from app.agents.common import llm_step, prompt, wrap_untrusted
from app.core import messaging, rules
from app.core.lang import LANG_NAMES, render_pair
from app.settings import clinic_config, settings
from app.tools import schemas as S
from app.tools.registry import ToolContext, ToolPermissionError, call_tool, text_hash

SEVERITY = ("injection", "impersonation", "other_recipient", "other_patient_data", "clinical", "complaint", "billing",
            "human_request", "abuse", "ai_unavailable", "low_confidence", "unusual_length", "opt_out")
ESC_CATEGORY = {"clinical": "clinical", "complaint": "complaint", "billing": "billing", "human_request": "human_request",
                "injection": "security", "impersonation": "security", "other_recipient": "security",
                "other_patient_data": "other_patient_data", "abuse": "abuse", "unusual_length": "security",
                "low_confidence": "low_confidence", "ai_unavailable": "ai_unavailable"}
HOLDING = {"clinical": "holding_clinical", "security": "holding_security", "other_patient_data": "holding_security",
           "human_request": "holding_human"}


def _holding_for(category: str, urgency: str, text: str) -> str:
    """995 wording only for symptoms or urgent cases; a routine clinical *question* gets the gentler reply."""
    if category == "clinical" and urgency == "routine" and "clinical" not in rules.prescreen(text, settings.max_inbound_chars).hits:
        return "holding_question"
    return HOLDING.get(category, "holding_general")
URGENT_WORDS = ["swollen", "swelling", "swell", "fever", "can't breathe", "trauma", "accident", "knocked out",
                "heavy bleeding", "肿", "发烧", "bengkak", "demam", "வீக்கம்", "காய்ச்சல்"]
_RANK = {"routine": 0, "soon": 1, "urgent": 2}


def _det_urgency(cats: list[str], text: str) -> str:
    if "clinical" in cats and rules.has_any(text, URGENT_WORDS):
        return "urgent"
    if set(cats) & {"clinical", "complaint", "injection", "impersonation", "other_recipient", "other_patient_data"}:
        return "soon"
    return "routine"


def _escalate(state: dict[str, Any], *, category: str, urgency: str, summary: str, holding: str | None) -> dict[str, Any]:
    run_id, pid = state["run_id"], state["patient_id"]
    lang = state.get("language", "en")
    ctx = ToolContext(run_id=run_id, agent="guard", patient_id=pid)
    res = call_tool("guard", "escalate_to_staff", {"patient_id": pid, "category": category, "urgency": urgency,
                                                   "summary_en": summary[:400]}, ctx,
                    thought_summary=f"Tier 3: {category} always goes to a human")
    if holding:
        text, gloss = render_pair(holding, lang)
        messaging.send_text(pid, text, ctx_patient_id=pid, run_id=run_id, agent="guard", gloss_en=gloss,
                            bypass_sensitive=True)       # pre-approved safety text: exempt from the Tier-2 hold
    return {"escalation": {"id": res["escalation_id"], "category": category, "urgency": urgency, "summary_en": summary}}


def guard_node(state: dict[str, Any]) -> dict[str, Any]:
    run_id, pid = state["run_id"], state["patient_id"]
    visited = state.get("visited", []) + ["guard"]
    lang = state.get("language", "en")

    # Mode 1: execute an escalation requested by the supervisor or another agent.
    req = state.get("escalation_request")
    if req and not state.get("escalation"):
        cat = req.get("category", "other")
        urg = req.get("urgency", "routine")
        upd = _escalate(state, category=cat, urgency=urg, summary=req.get("summary_en", cat),
                        holding=_holding_for(cat, urg, state.get("inbound_text") or ""))
        return {**upd, "visited": visited, "outcome": f"escalated:{cat}"}

    # Mode 2: screen an inbound message.
    text = state.get("inbound_text") or ""
    ps = rules.prescreen(text, settings.max_inbound_chars)
    trace.event(run_id, "guard", "prescreen", patient_id=pid, outcome="ok" if not ps.hits else "escalated",
                args={**ps.to_dict(), "chars": len(text)},
                thought_summary=f"deterministic hits: {', '.join(ps.categories) or 'none'}")

    llm_verdict: S.GuardVerdict | None = None
    source = "rules"
    if ps.too_long:
        cats = ["unusual_length"]
    elif ps.opt_out_exact:
        cats = ["opt_out"]
    elif ps.short_reply and not ps.hits:
        cats = []
    elif (ps.empty or ps.emoji_only) and not ps.hits:
        cats = ["unclear"]
    else:
        out = llm_step("guard", run_id=run_id, patient_id=pid,
                       system=prompt("guard", clinic=clinic_config()["clinic"]["name"],
                                     clinic_kind=clinic_config()["clinic"].get("kind", "clinic"),
                                     hits=", ".join(f"{k}: {v}" for k, v in ps.hits.items()) or "none"),
                       user=f"Patient's preferred language: {LANG_NAMES.get(lang, lang)}\n{wrap_untrusted(text)}",
                       allowed={"verdict": S.GuardVerdict}, context={"text": text, "lang": lang}, max_tokens=300)
        cats = list(ps.categories)
        if out.ok:
            llm_verdict = out.args
            source = "rules+llm"
            if llm_verdict.category not in ("safe", "unclear") and llm_verdict.category not in cats:
                cats.append(llm_verdict.category)
            if not cats and llm_verdict.category == "unclear":
                cats = ["unclear"]
            if not cats and llm_verdict.category == "safe" and llm_verdict.confidence < 0.6:
                cats = ["low_confidence"]
        elif not cats:
            # model unavailable (e.g. token budget reached) or unparseable, and no deterministic hit: fail safe
            cats = ["ai_unavailable" if (out.error or "").startswith("llm unavailable") else "low_confidence"]
            source = "rules (llm failed)"
    ordered = sorted(cats, key=lambda c: SEVERITY.index(c) if c in SEVERITY else -1)
    primary = ordered[0] if ordered else "safe"
    gloss = (llm_verdict.gloss_en if llm_verdict and llm_verdict.gloss_en else (text if lang == "en" else ""))
    urgency = _det_urgency(ordered, text)
    if llm_verdict and _RANK[llm_verdict.urgency] > _RANK[urgency] and primary not in ("safe", "unclear"):
        urgency = llm_verdict.urgency
    if state.get("inbound_message_id") and gloss:
        db.execute("UPDATE messages SET gloss_en=?, lang=? WHERE id=?", (gloss[:500], lang, state["inbound_message_id"]))

    action = {"safe": "allow", "unclear": "clarify", "opt_out": "opt_out"}.get(primary, "escalate")
    if "opt_out" in ordered and primary != "opt_out":
        action = "opt_out+escalate"
    verdict = {"category": primary, "categories": ordered, "action": action, "urgency": urgency, "source": source,
               "confidence": llm_verdict.confidence if llm_verdict else 1.0, "gloss_en": gloss[:300],
               "short_reply": ps.short_reply, "hits": ps.hits}
    trace.event(run_id, "guard", "verdict", patient_id=pid, guard_verdict=verdict,
                outcome="ok" if action == "allow" else ("blocked" if action == "clarify" else "escalated"),
                thought_summary=(llm_verdict.summary_en if llm_verdict else f"rules: {primary}"))

    update: dict[str, Any] = {"guard_verdict": verdict, "visited": visited}
    ctx = ToolContext(run_id=run_id, agent="guard", patient_id=pid)
    if action.startswith("opt_out"):
        call_tool("guard", "record_opt_out", {"patient_id": pid, "source_text_hash": text_hash(text)}, ctx,
                  thought_summary="opt-out honoured immediately and permanently")
        t, g = render_pair("opt_out_confirm", lang)
        messaging.send_text(pid, t, ctx_patient_id=pid, run_id=run_id, agent="guard", gloss_en=g,
                            allow_opted_out=True, bypass_sensitive=True)
        update["outcome"] = "opted_out"
        update["messages"] = [{"role": "patient", "text": "[opt-out request]"}]
        if action == "opt_out":
            return update
    if action == "clarify":
        visit = state.get("visit_label_local") or ""
        t, g = render_pair("clarify", lang, visit=visit or "check-up")
        messaging.send_text(pid, t, ctx_patient_id=pid, run_id=run_id, agent="guard", gloss_en=g)
        update["outcome"] = "clarify"
        update["messages"] = [{"role": "patient", "text": "[unclear message]"}, {"role": "clinic", "text": t}]
        return update
    if action == "allow":
        update["messages"] = [{"role": "patient", "text": text[:400]}]
        return update
    # escalate (possibly after an opt-out)
    esc_primary = next((c for c in ordered if c != "opt_out"), primary)
    esc_cat = ESC_CATEGORY.get(esc_primary, "other")
    hits = "; ".join(f"{k}={','.join(v)}" for k, v in ps.hits.items())
    summary = (f"{esc_primary.replace('_', ' ').title()} ({LANG_NAMES.get(lang, lang)}). "
               f"{(llm_verdict.summary_en + ' ') if llm_verdict and llm_verdict.summary_en else ''}"
               f"Patient wrote: \"{(gloss or text)[:200]}\"{'. Pre-screen: ' + hits if hits else ''}")
    try:
        esc = _escalate(state, category=esc_cat, urgency=urgency, summary=summary,
                        holding=None if action == "opt_out+escalate" else _holding_for(esc_cat, urgency, text))
    except ToolPermissionError:
        esc = {}
    update.update(esc)
    update["outcome"] = f"escalated:{esc_cat}"
    update["messages"] = [{"role": "patient", "text": f"[withheld by safety guard: {esc_primary}]"}]
    return update
