"""Scheduling agent: the ONLY calendar writer. The LLM reads the patient's time preference into a typed FindSlots
request; slot search, booking and cancellation are deterministic and scoped to the current patient."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app import clock, trace
from app.agents.common import llm_step, prompt, wrap_untrusted
from app.core import rules, scheduling
from app.core.lang import fmt_when
from app.llm.protocol import tool_block
from app.settings import clinic_config
from app.tools import schemas as S
from app.tools.registry import ToolContext, ToolPermissionError, call_tool, own_context


def _plan_find(state: dict[str, Any], pref_text: str, duration: int) -> dict[str, Any]:
    """Model reads the preference; deterministic parser is the fallback. Duration always comes from config."""
    run_id, pid = state["run_id"], state["patient_id"]
    today = clock.today()
    horizon = clinic_config()["calendar"]["horizon_days"]
    if pref_text.strip():
        # The model picks dates from this list instead of doing weekday arithmetic (live runs showed it slipping).
        calendar = ", ".join((today + timedelta(days=i)).strftime("%a %Y-%m-%d") for i in range(1, horizon + 1))
        out = llm_step("scheduling", run_id=run_id, patient_id=pid,
                       system=prompt("scheduling", clinic=clinic_config()["clinic"]["name"], today=today.isoformat(),
                                     weekday=today.strftime("%A"), first_day=(today + timedelta(days=1)).isoformat(),
                                     last_day=(today + timedelta(days=horizon)).isoformat(), duration=duration,
                                     calendar=calendar,
                                     hours=clinic_config()["info_sheet"]["hours"],
                                     tools=tool_block({"find_slots": S.FindSlots})),
                       user=wrap_untrusted(pref_text), allowed={"find_slots": S.FindSlots},
                       context={"preference_text": pref_text, "duration_min": duration}, max_tokens=200)
        if out.ok:
            args = out.args.model_dump(mode="json")
            args["duration_min"] = duration
            return args
        trace.event(run_id, "scheduling", "fallback_parser", patient_id=pid, args={"why": out.error},
                    thought_summary="model output unusable; deterministic time parser used")
    args = rules.parse_time_preference(pref_text)
    args.pop("_matched", None)
    args["duration_min"] = duration
    return args


def scheduling_node(state: dict[str, Any]) -> dict[str, Any]:
    run_id, pid = state["run_id"], state["patient_id"]
    req = state.get("pending_scheduling") or {}
    intent = req.get("intent", "find_times")
    ci = own_context(pid)
    visit_type = ci["visit_type"]
    duration = scheduling.duration_for(visit_type)
    pending = state.get("proposed_slots") or []
    booking = scheduling.active_booking(pid)
    ctx = ToolContext(run_id=run_id, agent="scheduling", patient_id=pid,
                      offered_slot_ids=[o["slot_id"] for o in pending],
                      replacing_appointment_id=state.get("rescheduling_appointment_id"))
    upd: dict[str, Any] = {"pending_scheduling": None, "scheduling_rendered": False,
                           "visited": state.get("visited", []) + ["scheduling"]}

    if intent == "cancel":
        if not booking:
            return {**upd, "scheduling_result": {"type": "no_booking"}}
        call_tool("scheduling", "cancel_own_booking", {"appointment_id": booking["id"], "patient_id": pid}, ctx,
                  thought_summary="patient asked to cancel their own booking")
        return {**upd, "scheduling_result": {"type": "cancelled", "start_ts": booking["start_ts"]},
                "proposed_slots": [], "rescheduling_appointment_id": None}

    if intent == "choose_option":
        n = req.get("option_number") or 0
        if not pending or n < 1 or n > len(pending):
            return {**upd, "scheduling_result": {"type": "invalid_option"}}
        slot = pending[n - 1]
        if clock.parse(slot["start_ts"]) < clock.now() + timedelta(hours=scheduling.BOOKING_MIN_NOTICE_HOURS):
            # The patient answered days later: the offered times have passed. Offer fresh ones, never book the past.
            today = clock.today()
            horizon = clinic_config()["calendar"]["horizon_days"]
            found = call_tool("scheduling", "find_slots", {"earliest": today.isoformat(),
                                                           "latest": (today + timedelta(days=horizon)).isoformat(),
                                                           "part_of_day": "any", "duration_min": duration}, ctx,
                              thought_summary="offered times have passed; offering the next available times")
            if not found["options"]:
                return {**upd, "scheduling_result": {"type": "no_slots"}, "proposed_slots": []}
            return {**upd, "scheduling_result": {"type": "options_expired", "options": found["options"]},
                    "proposed_slots": found["options"]}
        try:
            b = call_tool("scheduling", "book_slot", {"slot_id": slot["slot_id"], "patient_id": pid,
                                                      "visit_type": visit_type}, ctx,
                          thought_summary=f"patient chose option {n}")
        except scheduling.BookingError as e:
            if "limit" in str(e):
                return {**upd, "scheduling_result": {"type": "booking_limit"}}
            d = clock.parse(slot["start_ts"]).date()
            found = call_tool("scheduling", "find_slots", {"earliest": d.isoformat(), "latest": (d + timedelta(days=3)).isoformat(),
                                                           "part_of_day": "any", "duration_min": duration}, ctx,
                              thought_summary="chosen slot was taken; offering nearby times")
            return {**upd, "scheduling_result": {"type": "slot_taken", "options": found["options"]},
                    "proposed_slots": found["options"]}
        kind = "rescheduled" if ctx.replacing_appointment_id else "booked"
        return {**upd, "scheduling_result": {"type": kind, "booking": b}, "proposed_slots": [],
                "rescheduling_appointment_id": None, "booking": b}

    # find_times | reschedule
    resched_id = None
    if intent == "reschedule" and booking:
        resched_id = booking["id"]
    elif booking:
        when = clock.parse(booking["start_ts"])
        return {**upd, "scheduling_result": {"type": "already_booked", "booking": {
            "when": fmt_when(when, "en"), "when_local": fmt_when(when, state.get("language", "en")),
            "dentist": scheduling.dentist_name(booking["dentist_id"])}}}
    args = _plan_find(state, req.get("preference_text") or "", duration)
    try:
        found = call_tool("scheduling", "find_slots", args, ctx, thought_summary="deterministic slot search")
    except ToolPermissionError:
        fallback = rules.parse_time_preference("")
        fallback.pop("_matched", None)
        found = call_tool("scheduling", "find_slots", {**fallback, "duration_min": duration}, ctx,
                          thought_summary="invalid search window; searched the whole horizon")
    if not found["options"]:
        return {**upd, "scheduling_result": {"type": "no_slots"}, "proposed_slots": []}
    return {**upd, "scheduling_result": {"type": "offer", "exact": found["exact_match"], "options": found["options"]},
            "proposed_slots": found["options"], "rescheduling_appointment_id": resched_id}
