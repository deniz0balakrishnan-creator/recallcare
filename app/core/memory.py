"""Long-term per-patient memory: the follow-up state machine, contact times, 24h window, rolling English summary."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app import clock, db
from app.settings import clinic_config

STATES = ("due", "proposed", "approved", "contacted", "replied", "booked", "declined", "escalated", "opted_out",
          "no_response")
TRANSITIONS: dict[str, set[str]] = {
    "due": {"proposed", "opted_out", "escalated"},
    "proposed": {"approved", "due", "opted_out", "escalated"},
    "approved": {"contacted", "due", "opted_out", "escalated"},
    "contacted": {"replied", "no_response", "opted_out", "escalated", "booked", "declined"},
    "replied": {"booked", "declined", "escalated", "opted_out", "replied"},
    "booked": {"replied", "booked", "declined", "escalated", "opted_out"},
    "declined": {"replied", "due", "opted_out", "escalated", "booked"},
    "escalated": {"replied", "booked", "declined", "opted_out", "due", "escalated"},
    "opted_out": set(),                       # permanent
    "no_response": {"replied", "due", "opted_out", "escalated"},
}


class TransitionError(Exception):
    pass


def get(patient_id: int) -> dict[str, Any] | None:
    f = db.q1("SELECT * FROM followups WHERE patient_id=?", (patient_id,))
    if f and f.get("pending_options"):
        f["pending_options"] = json.loads(f["pending_options"])
    return f


def upsert(patient_id: int, **fields: Any) -> None:
    fields["updated_at"] = clock.iso(clock.now())
    if "pending_options" in fields and fields["pending_options"] is not None:
        fields["pending_options"] = db.dumps(fields["pending_options"])
    existing = db.q1("SELECT patient_id FROM followups WHERE patient_id=?", (patient_id,))
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE followups SET {sets} WHERE patient_id=?", (*fields.values(), patient_id))
    else:
        fields.setdefault("status", "due")
        cols = ["patient_id", *fields]
        db.execute(f"INSERT INTO followups({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                   (patient_id, *fields.values()))


def transition(patient_id: int, new: str, *, force: bool = False, **fields: Any) -> str:
    """Move the state machine; illegal moves raise unless forced (staff actions). Returns the previous state."""
    cur = get(patient_id)
    old = cur["status"] if cur else "due"
    if old == "opted_out" and new != "opted_out":
        raise TransitionError("opted_out is permanent")
    if not force and new != old and new not in TRANSITIONS.get(old, set()):
        raise TransitionError(f"{old} -> {new} not allowed")
    upsert(patient_id, status=new, **fields)
    return old


def note(patient_id: int, line: str, max_lines: int = 8) -> None:
    """Append one dated line to the rolling English summary (deterministic, short)."""
    cur = get(patient_id)
    lines = [ln for ln in (cur["summary_en"] if cur else "").split("\n") if ln]
    lines.append(f"{clock.now().strftime('%d %b %H:%M')} {line[:140]}")
    upsert(patient_id, summary_en="\n".join(lines[-max_lines:]))


def open_window(patient_id: int) -> None:
    hours = clinic_config()["outreach"]["window_hours"]
    now = clock.now()
    upsert(patient_id, last_inbound_at=clock.iso(now), window_expires_at=clock.iso(now + timedelta(hours=hours)))


def window_open(patient_id: int) -> bool:
    f = get(patient_id)
    exp = clock.parse(f["window_expires_at"]) if f else None
    return bool(exp and exp > clock.now())
