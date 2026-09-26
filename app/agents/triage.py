"""Triage agent: deterministic ranking; the LLM only writes staff-facing reasons (and can't change membership)."""
from __future__ import annotations

from typing import Any

from app import clock, trace
from app.agents.common import llm_step, prompt
from app.core.recall import deterministic_reason
from app.llm.protocol import tool_block
from app.settings import clinic_config
from app.tools import schemas as S
from app.tools.registry import ToolContext, call_tool


def triage_node(state: dict[str, Any]) -> dict[str, Any]:
    run_id = state["run_id"]
    ctx = ToolContext(run_id=run_id, agent="triage")
    size = clinic_config()["outreach"]["daily_batch_size"]
    due = call_tool("triage", "list_patients_due", {"as_of": clock.today().isoformat(), "limit": size}, ctx,
                    thought_summary="deterministic overdue + urgency ranking")
    if not due:
        return {"batch": [], "outcome": "nothing due", "visited": state.get("visited", []) + ["triage"]}
    ids = [d["patient_id"] for d in due]
    for d in due:
        if d["sensitive"]:
            call_tool("triage", "get_patient_summary", {"patient_id": d["patient_id"]}, ctx,
                      thought_summary="sensitive flag → Tier 2 note")
    items = [{"patient_id": d["patient_id"], "facts": d["facts"]} for d in due]
    lines = "\n".join(f"{d['patient_id']}: {'; '.join(d['facts'])} [urgency {d['urgency']}]" for d in due)
    out = llm_step("triage", run_id=run_id, patient_id=None,
                   system=prompt("triage", clinic=clinic_config()["clinic"]["name"],
                                 tools=tool_block({"propose_batch": S.ProposeBatch})),
                   user=f"Today: {clock.today().isoformat()}\nRANKED PATIENTS (id: facts):\n{lines}",
                   allowed={"propose_batch": S.ProposeBatch}, context={"items": items}, max_tokens=900)
    reasons: dict[int, str]
    if out.ok and set(out.args.patient_ids) == set(ids):
        reasons = {pid: out.args.reasons[pid] for pid in ids}
    else:
        why = out.error or "model changed batch membership"
        trace.event(run_id, "triage", "fallback_reasons", outcome="ok",
                    args={"why": why}, thought_summary="kept deterministic ranking; used rule-based reasons")
        reasons = {d["patient_id"]: deterministic_reason_from(d) for d in due}
    for d in due:
        if d["sensitive"]:
            reasons[d["patient_id"]] = (reasons[d["patient_id"]] + " · Tier 2: staff approve every message")[:200]
    call_tool("triage", "propose_batch", {"patient_ids": ids, "reasons": reasons}, ctx,
              thought_summary=f"proposed {len(ids)} patients for staff approval")
    batch = [{**d, "reason": reasons[d["patient_id"]]} for d in due]
    return {"batch": batch, "outcome": f"proposed {len(batch)}", "visited": state.get("visited", []) + ["triage"]}


def deterministic_reason_from(d: dict[str, Any]) -> str:
    return "; ".join(d["facts"])[:200]
