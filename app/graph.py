"""LangGraph orchestration: a supervisor routes between four specialists based on explicit, typed state.

    START → supervisor ─┬─► triage ──────┐
                        ├─► guard ───────┤
                        ├─► conversation ┤──► supervisor … → END
                        └─► scheduling ──┘

Short-term memory: SQLite checkpointer, thread_id = patient-<id> (or triage-<date>).
Long-term memory: the followups table (state machine + rolling summary) in app DB.
Hard cap: max_steps_per_run supervisor steps → escalate "loop limit".
"""
from __future__ import annotations

import argparse
import sqlite3
import threading
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app import trace
from app.agents.conversation import conversation_node
from app.agents.guard import guard_node
from app.agents.scheduling import scheduling_node
from app.agents.triage import triage_node
from app.settings import settings


def _window(old: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """Bounded conversation window (short-term memory)."""
    merged = list(old or []) + list(new or [])
    return merged[-settings.message_window:]


class RCState(TypedDict, total=False):
    # event + identity
    event_type: Literal["daily_triage", "outreach", "renudge", "inbound_message"]
    run_id: str
    thread_id: str
    patient_id: int | None
    language: str
    visit_label_local: str
    # inbound
    inbound_text: str | None
    inbound_message_id: int | None
    guard_verdict: dict[str, Any] | None
    # hand-offs between agents
    pending_scheduling: dict[str, Any] | None
    scheduling_result: dict[str, Any] | None
    scheduling_rendered: bool
    escalation_request: dict[str, Any] | None
    escalation: dict[str, Any] | None
    # persisted across turns (checkpointer)
    proposed_slots: list[dict[str, Any]]
    rescheduling_appointment_id: int | None
    booking: dict[str, Any] | None
    messages: Annotated[list[dict[str, Any]], _window]
    # control
    batch: list[dict[str, Any]] | None
    route: str | None
    visited: list[str]
    step_count: int
    outcome: str | None


SPECIALISTS = ("triage", "guard", "conversation", "scheduling")


def _decide(state: RCState) -> str:
    ev = state["event_type"]
    visited = state.get("visited") or []
    if ev == "daily_triage":
        return "triage" if "triage" not in visited else "end"
    if ev in ("outreach", "renudge"):
        return "conversation" if "conversation" not in visited else "end"
    # inbound_message
    if state.get("escalation"):
        return "end"
    if state.get("escalation_request"):
        return "guard"
    verdict = state.get("guard_verdict")
    if not verdict:
        return "guard"
    if verdict["action"] != "allow":
        return "end"
    if state.get("pending_scheduling"):
        return "scheduling"
    if state.get("scheduling_result") and not state.get("scheduling_rendered"):
        return "conversation"
    if "conversation" not in visited:
        return "conversation"
    return "end"


def supervisor_node(state: RCState) -> dict[str, Any]:
    step = (state.get("step_count") or 0) + 1
    route = _decide(state)
    upd: dict[str, Any] = {"step_count": step, "route": route}
    if step > settings.max_steps_per_run and route != "end":
        if state.get("escalation_request") or state.get("escalation"):
            route = "end"
        else:
            route = "guard"
            upd["escalation_request"] = {"category": "loop_limit", "urgency": "routine",
                                         "summary_en": f"Run exceeded {settings.max_steps_per_run} steps; handing to staff."}
        upd["route"] = route
    trace.event(state["run_id"], "supervisor", f"route → {route}", patient_id=state.get("patient_id"),
                args={"step": step, "event": state["event_type"]},
                outcome="escalated" if upd.get("escalation_request") else "ok",
                thought_summary=_why(state, route))
    return upd


def _why(state: RCState, route: str) -> str:
    v = state.get("guard_verdict")
    if route == "guard" and not v:
        return "every inbound message is screened by the guard first"
    if route == "guard":
        return "escalation requested → guard hands over to staff"
    if route == "scheduling":
        return "conversation handed off a scheduling request"
    if route == "conversation" and state.get("scheduling_result"):
        return "render scheduling result for the patient"
    if route == "end" and v and v["action"] != "allow":
        return f"guard verdict {v['category']} → run ends (no other agent sees the message)"
    return f"{state['event_type']} → {route}"


def build(checkpointer=None):
    g = StateGraph(RCState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("triage", triage_node)
    g.add_node("guard", guard_node)
    g.add_node("conversation", conversation_node)
    g.add_node("scheduling", scheduling_node)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", lambda s: s["route"],
                            {"triage": "triage", "guard": "guard", "conversation": "conversation",
                             "scheduling": "scheduling", "end": END})
    for n in SPECIALISTS:
        g.add_edge(n, "supervisor")
    return g.compile(checkpointer=checkpointer)


_graphs: dict[str, Any] = {}
_lock = threading.Lock()
RUN_LOCK = threading.RLock()        # one graph run at a time (small clinic volume; keeps SQLite simple)


def get_graph():
    path = str(settings.path(settings.checkpoint_db_path))
    with _lock:
        if path not in _graphs:
            settings.path(settings.checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            _graphs[path] = build(SqliteSaver(conn))
        return _graphs[path]


def mermaid() -> str:
    return build().get_graph().draw_mermaid()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mermaid", action="store_true")
    a = ap.parse_args()
    if a.mermaid:
        print(mermaid())


if __name__ == "__main__":
    main()
