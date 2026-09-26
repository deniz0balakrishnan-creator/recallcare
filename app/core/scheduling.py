"""Deterministic calendar logic: slot search, booking, cancellation. The LLM never picks a time itself."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

from app import clock, db
from app.settings import clinic_config


# Offers start at least OFFER_LEAD_HOURS ahead; a chosen slot is still bookable until BOOKING_MIN_NOTICE_HOURS before
# it starts. The gap is the time a patient has to reply before an offered option expires.
OFFER_LEAD_HOURS = 3
BOOKING_MIN_NOTICE_HOURS = 1


class BookingError(Exception):
    """Raised for any booking rule violation. Message is safe to log (no patient text)."""


def dentist_name(dentist_id: str) -> str:
    for d in clinic_config()["clinic"]["dentists"]:
        if d["id"] == dentist_id:
            return d["name"]
    return dentist_id


def duration_for(visit_type: str) -> int:
    return int(clinic_config()["visit_types"].get(visit_type, {}).get("duration_min", 30))


def _part_bounds(part: str) -> tuple[str, str]:
    if part == "any":
        return "00:00", "23:59"
    return tuple(clinic_config()["calendar"]["parts_of_day"][part])  # type: ignore[return-value]


def _chain_ids(start_slot_id: str, n: int) -> list[str]:
    ts, did = start_slot_id.split("|")
    t = datetime.fromisoformat(ts)
    step = clinic_config()["calendar"]["slot_minutes"]
    return [f"{(t + timedelta(minutes=step * k)).strftime('%Y-%m-%dT%H:%M')}|{did}" for k in range(n)]


def _free_chain_starts(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    free = {r["id"] for r in rows if r["status"] == "free"}
    return [r for r in rows if r["status"] == "free" and all(cid in free for cid in _chain_ids(r["id"], n))]


def find_slots(earliest: date, latest: date, part_of_day: str = "any", duration_min: int = 30,
               weekdays: list[int] | None = None, limit: int | None = None,
               not_before: datetime | None = None) -> list[dict[str, Any]]:
    """Up to `limit` options, spread across different days first, then earliest-first."""
    cal = clinic_config()["calendar"]
    limit = limit or cal["max_options_offered"]
    not_before = not_before or (clock.now() + timedelta(hours=OFFER_LEAD_HOURS))
    rows = db.q("SELECT id, dentist_id, start_ts, status FROM slots WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts, dentist_id",
                (clock.iso(clock.at(earliest, "00:00")), clock.iso(clock.at(latest + timedelta(days=1), "00:00"))))
    n = math.ceil(duration_min / cal["slot_minutes"])
    lo, hi = _part_bounds(part_of_day)
    candidates = []
    for r in _free_chain_starts(rows, n):
        start = clock.parse(r["start_ts"])
        if start < not_before:
            continue
        end = start + timedelta(minutes=duration_min)
        if not (lo <= start.strftime("%H:%M") and end.strftime("%H:%M") <= hi or (hi == "23:59")):
            continue
        if weekdays and start.weekday() not in weekdays:
            continue
        candidates.append(r)
    picked: list[dict[str, Any]] = []
    seen_days: set[str] = set()
    seen_times: set[str] = set()
    for r in candidates:                     # first pass: one per day
        d = r["start_ts"][:10]
        if d not in seen_days:
            picked.append(r)
            seen_days.add(d)
            seen_times.add(r["start_ts"])
        if len(picked) >= limit:
            break
    for r in candidates:                     # second pass: fill up with distinct times (not the same time, other dentist)
        if len(picked) >= limit:
            break
        if r not in picked and r["start_ts"] not in seen_times:
            picked.append(r)
            seen_times.add(r["start_ts"])
    picked.sort(key=lambda r: r["start_ts"])
    return [{"slot_id": r["id"], "start_ts": r["start_ts"], "dentist_id": r["dentist_id"],
             "dentist": dentist_name(r["dentist_id"]), "duration_min": duration_min} for r in picked]


def find_slots_with_fallback(earliest: date, latest: date, part_of_day: str, duration_min: int,
                             weekdays: list[int] | None = None) -> tuple[list[dict[str, Any]], bool]:
    """(options, exact_match). Widens deterministically: drop part-of-day → drop weekday → whole horizon."""
    opts = find_slots(earliest, latest, part_of_day, duration_min, weekdays)
    if opts:
        return opts, True
    horizon_end = clock.today() + timedelta(days=clinic_config()["calendar"]["horizon_days"])
    for args in ((earliest, latest, "any", weekdays), (earliest, latest + timedelta(days=3), part_of_day, None),
                 (clock.today(), horizon_end, part_of_day, None), (clock.today(), horizon_end, "any", None)):
        opts = find_slots(args[0], args[1], args[2], duration_min, args[3])
        if opts:
            return opts, False
    return [], False


def active_booking(patient_id: int) -> dict[str, Any] | None:
    return db.q1("SELECT * FROM appointments WHERE patient_id=? AND status='booked' AND start_ts>=? ORDER BY start_ts LIMIT 1",
                 (patient_id, clock.iso(clock.now())))


def book(slot_id: str, patient_id: int, visit_type: str, run_id: str | None, created_by: str = "agent:scheduling",
         replacing_appointment_id: int | None = None) -> dict[str, Any]:
    cal = clinic_config()["calendar"]
    duration = duration_for(visit_type)
    chain = _chain_ids(slot_id, math.ceil(duration / cal["slot_minutes"]))
    with db.tx() as c:
        active = c.execute("SELECT id FROM appointments WHERE patient_id=? AND status='booked' AND start_ts>=?",
                           (patient_id, clock.iso(clock.now()))).fetchall()
        active_ids = {r["id"] for r in active} - ({replacing_appointment_id} if replacing_appointment_id else set())
        if len(active_ids) >= cal["max_active_bookings_per_patient"]:
            raise BookingError("booking limit reached")
        rows = c.execute(f"SELECT id, status, start_ts, dentist_id FROM slots WHERE id IN ({','.join('?' * len(chain))})",
                         chain).fetchall()
        if len(rows) != len(chain) or any(r["status"] != "free" for r in rows):
            raise BookingError("slot not available")
        first = min(rows, key=lambda r: r["start_ts"])
        if clock.parse(first["start_ts"]) < clock.now() + timedelta(hours=BOOKING_MIN_NOTICE_HOURS):
            raise BookingError("slot is in the past or too soon")
        cur = c.execute(
            "INSERT INTO appointments(patient_id,dentist_id,start_ts,duration_min,visit_type,status,created_by,created_at,run_id)"
            " VALUES(?,?,?,?,?,'booked',?,?,?)",
            (patient_id, first["dentist_id"], first["start_ts"], duration, visit_type, created_by,
             clock.iso(clock.now()), run_id))
        appt_id = cur.lastrowid
        c.executemany("UPDATE slots SET status='booked', appointment_id=? WHERE id=?", [(appt_id, cid) for cid in chain])
        if replacing_appointment_id:
            _cancel_in_tx(c, replacing_appointment_id, patient_id)
    return {"appointment_id": appt_id, "start_ts": first["start_ts"], "dentist_id": first["dentist_id"],
            "dentist": dentist_name(first["dentist_id"]), "duration_min": duration, "visit_type": visit_type}


def _cancel_in_tx(c, appointment_id: int, patient_id: int) -> None:
    row = c.execute("SELECT patient_id, status FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not row or row["patient_id"] != patient_id:
        raise BookingError("appointment does not belong to this patient")
    if row["status"] != "booked":
        raise BookingError("appointment is not active")
    c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))
    c.execute("UPDATE slots SET status='free', appointment_id=NULL WHERE appointment_id=?", (appointment_id,))


def cancel_own(appointment_id: int, patient_id: int) -> None:
    with db.tx() as c:
        _cancel_in_tx(c, appointment_id, patient_id)
