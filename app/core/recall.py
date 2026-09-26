"""Deterministic overdue + urgency scoring. No LLM involved — every number here is explainable."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from app import clock, db
from app.settings import clinic_config

# Follow-up states that mean "someone is already handling this patient".
ACTIVE_STATES = {"proposed", "approved", "contacted", "replied", "booked", "escalated"}
CLOSED_STATES = {"declined", "opted_out", "no_response"}


@dataclass
class DueItem:
    patient_id: int
    name: str
    preferred_language: str
    age: int
    chas_tier: str
    generation_card: str
    visit_type: str
    due_date: str
    days_overdue: int
    months_overdue: float
    score: float
    urgency: str
    facts: list[str]
    sensitive: bool
    consent: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def age_on(birth_iso: str, today: date) -> int:
    b = date.fromisoformat(birth_iso)
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def score_patient(*, months_overdue: float, visit_type: str, age: int, chas_tier: str) -> tuple[float, str]:
    u = clinic_config()["urgency"]
    months = max(0.0, months_overdue)
    s = min(months, u["overdue_month_cap"]) * u["per_overdue_month"]
    s += u["visit_type_bonus"].get(visit_type, 0)
    if age >= 65:
        s += u["age_65_plus"]
    if chas_tier != "none":
        s += u["chas_cardholder"]
    if months > u["long_gap_bonus_months"]:
        s += u["long_gap_bonus"]
    if months_overdue < 0:        # due soon, not yet overdue: only rank after the overdue ones
        s = min(s, u["levels"]["soon"] - 1)
    levels = u["levels"]
    urgency = "urgent" if s >= levels["urgent"] else ("soon" if s >= levels["soon"] else "routine")
    return round(s, 1), urgency


_LANG_LABEL = {"en": "English", "zh": "Mandarin", "ms": "Malay", "ta": "Tamil"}


def _facts(item: dict[str, Any], months: float, age: int) -> list[str]:
    vt = clinic_config()["visit_types"][item["visit_type"]]["label"]
    facts = []
    if months >= 1:
        facts.append(f"{vt} overdue by {months:.0f} month{'s' if round(months) != 1 else ''}")
    elif months >= 0:
        facts.append(f"{vt} due now")
    else:
        facts.append(f"{vt} due in {-months * 30.44:.0f} days")
    if item["source"] == "treatment_plan":
        facts.append("unfinished treatment plan")
    if age >= 65:
        facts.append(f"age {age}")
    if item["chas_tier"] != "none":
        facts.append(f"CHAS {item['chas_tier'].title()}")
    if item["generation_card"] != "none":
        facts.append(f"{item['generation_card'].title()} Generation")
    if item["preferred_language"] != "en":
        facts.append(f"prefers {_LANG_LABEL[item['preferred_language']]}")
    return facts


def list_due(as_of: date | None = None, limit: int = 50, include_no_consent: bool = False) -> list[DueItem]:
    """Patients overdue (or due within `due_soon_days`) who nobody is currently handling, highest score first."""
    as_of = as_of or clock.today()
    u = clinic_config()["urgency"]
    rows = db.q(
        """
        SELECT r.patient_id, r.visit_type, r.due_date, r.source,
               p.full_name, p.preferred_language, p.birth_date, p.chas_tier, p.generation_card,
               p.whatsapp_consent, p.sensitive, p.opted_out,
               f.status AS fstatus, f.due_date AS fdue
        FROM recalls r
        JOIN patients p ON p.id = r.patient_id
        LEFT JOIN followups f ON f.patient_id = r.patient_id
        WHERE r.active = 1 AND p.opted_out = 0
          AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.patient_id = r.patient_id
                          AND a.status = 'booked' AND a.start_ts >= ?)
        """,
        (clock.iso(clock.now()),),
    )
    out: list[DueItem] = []
    for r in rows:
        due = date.fromisoformat(r["due_date"])
        days = (as_of - due).days
        if days < -u["due_soon_days"]:
            continue
        if r["fstatus"] in ACTIVE_STATES:
            continue
        if r["fstatus"] in CLOSED_STATES and r["fdue"] == r["due_date"]:
            continue        # already worked this recall cycle
        if not r["whatsapp_consent"] and not include_no_consent:
            continue
        age = age_on(r["birth_date"], as_of)
        months = days / 30.44
        score, urgency = score_patient(months_overdue=months, visit_type=r["visit_type"], age=age,
                                       chas_tier=r["chas_tier"])
        out.append(DueItem(
            patient_id=r["patient_id"], name=r["full_name"], preferred_language=r["preferred_language"], age=age,
            chas_tier=r["chas_tier"], generation_card=r["generation_card"], visit_type=r["visit_type"],
            due_date=r["due_date"], days_overdue=days, months_overdue=round(months, 1), score=score, urgency=urgency,
            facts=_facts(r, months, age), sensitive=bool(r["sensitive"]), consent=bool(r["whatsapp_consent"]),
        ))
    out.sort(key=lambda x: (-x.score, -x.days_overdue, x.patient_id))
    return out[:limit]


def deterministic_reason(item: DueItem) -> str:
    return "; ".join(item.facts)[:200]


def no_consent_overdue(as_of: date | None = None) -> list[DueItem]:
    """Overdue patients we may NOT message (no WhatsApp consent) — shown to staff as a manual call list."""
    return [d for d in list_due(as_of, limit=200, include_no_consent=True) if not d.consent and d.days_overdue >= 0]
