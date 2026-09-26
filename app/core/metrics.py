"""Impact metrics — equity outcomes first, vanity counts last. All computed from the app DB (synthetic data)."""
from __future__ import annotations

from datetime import date
from typing import Any

from app import clock, db, trace
from app.core.recall import age_on
from app.settings import clinic_config

REACHED_STATES = ("contacted", "replied", "booked", "declined", "escalated", "opted_out", "no_response")
# Assumption: public list prices per million tokens (input, output), for the token-spend estimate only.
PRICES = {"gateway": (3.0, 15.0), "openrouter": (1.0, 5.0), "mock": (0.0, 0.0)}

GROUPS = [
    ("all", "All overdue patients"),
    ("gap12", "Overdue > 12 months"),
    ("age65", "Aged 65+"),
    ("non_en", "Prefers Mandarin, Malay or Tamil"),
    ("chas", "CHAS cardholder"),
]


def _in_group(key: str, row: dict[str, Any], today: date) -> bool:
    if key == "all":
        return True
    if key == "gap12":
        return row["due_date"] is not None and (today - date.fromisoformat(row["due_date"])).days > 365
    if key == "age65":
        return age_on(row["birth_date"], today) >= 65
    if key == "non_en":
        return row["preferred_language"] != "en"
    if key == "chas":
        return row["chas_tier"] != "none"
    return False


def equity_table() -> list[dict[str, Any]]:
    today = clock.today()
    rows = db.q("""SELECT f.patient_id, f.status, f.due_date, p.birth_date, p.preferred_language, p.chas_tier,
                          EXISTS(SELECT 1 FROM messages m WHERE m.patient_id=f.patient_id AND m.direction='out'
                                 AND m.kind='template' AND m.status='sent') AS reached,
                          EXISTS(SELECT 1 FROM messages m WHERE m.patient_id=f.patient_id AND m.direction='in') AS replied,
                          EXISTS(SELECT 1 FROM appointments a WHERE a.patient_id=f.patient_id AND a.status='booked'
                                 AND a.created_by='agent:scheduling') AS booked
                   FROM followups f JOIN patients p ON p.id=f.patient_id""")
    out = []
    for key, label in GROUPS:
        g = [r for r in rows if _in_group(key, r, today)]
        reached = [r for r in g if r["reached"]]
        replied = [r for r in reached if r["replied"]]
        booked = [r for r in reached if r["booked"]]
        out.append({"key": key, "label": label, "identified": len(g), "reached": len(reached),
                    "replied": len(replied), "booked": len(booked),
                    "rebook_rate": round(100 * len(booked) / len(reached)) if reached else None})
    return out


def escalation_counts() -> dict[str, int]:
    return {r["category"]: r["n"] for r in db.q("SELECT category, COUNT(*) n FROM escalations GROUP BY 1")}


def staff_minutes_saved() -> dict[str, Any]:
    a = clinic_config()["impact_assumptions"]
    templates = db.q1("SELECT COUNT(*) n FROM messages WHERE direction='out' AND kind='template' AND status='sent'")["n"]
    bookings = db.q1("SELECT COUNT(*) n FROM appointments WHERE created_by='agent:scheduling'")["n"]
    logistics = db.q1("SELECT COUNT(*) n FROM trace_events WHERE action='tool:get_clinic_info' AND outcome='ok'")["n"]
    triaged = db.q1("SELECT COUNT(*) n FROM trace_events WHERE action='tool:propose_batch' AND outcome='ok'")["n"]
    batch_size = clinic_config()["outreach"]["daily_batch_size"]
    parts = [
        ("Reminders sent", templates, a["minutes_per_manual_outreach"]),
        ("Bookings made in chat", bookings, a["minutes_per_booking_conversation"]),
        ("Logistics questions answered", logistics, a["minutes_per_logistics_answer"]),
        ("Patient records reviewed by triage", triaged * batch_size, a["minutes_per_triage_patient_review"]),
    ]
    total = sum(n * m for _, n, m in parts)
    return {"total": round(total), "parts": [{"label": l, "count": n, "minutes_each": m, "minutes": round(n * m, 1)}
                                             for l, n, m in parts]}


def token_spend() -> dict[str, Any]:
    rows = db.q("""SELECT model, COUNT(*) calls, COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout,
                          MAX(tokens_estimated) est
                   FROM trace_events WHERE model IS NOT NULL GROUP BY model""")
    total_cost = 0.0
    for r in rows:
        provider = (r["model"] or "").split(":")[0]
        pin, pout = PRICES.get(provider, (0.0, 0.0))
        r["cost"] = round(r["tin"] / 1e6 * pin + r["tout"] / 1e6 * pout, 4)
        total_cost += r["cost"]
    calls = sum(r["calls"] for r in rows)
    tin = sum(r["tin"] for r in rows)
    tout = sum(r["tout"] for r in rows)
    convs = db.q1("SELECT COUNT(DISTINCT patient_id) n FROM runs WHERE event_type='inbound_message'")["n"]
    return {"by_model": rows, "calls": calls, "tokens_in": tin, "tokens_out": tout, "cost": round(total_cost, 4),
            "conversations": convs, "tokens_per_conversation": round((tin + tout) / convs) if convs else None,
            "totals": trace.token_totals()}


def headline() -> dict[str, Any]:
    eq = {r["key"]: r for r in equity_table()}
    esc = escalation_counts()
    return {"reached": eq["all"]["reached"], "booked": eq["all"]["booked"], "rebook_rate": eq["all"]["rebook_rate"],
            "clinical_escalations": esc.get("clinical", 0), "escalations": sum(esc.values()),
            "opted_out": db.q1("SELECT COUNT(*) n FROM patients WHERE opted_out=1")["n"],
            "minutes_saved": staff_minutes_saved()["total"]}
