"""Single source of "now" (Asia/Singapore). Evals and the demo can freeze or advance time."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.settings import settings

SGT = ZoneInfo("Asia/Singapore")

_frozen: datetime | None = None
_offset = timedelta(0)


def now() -> datetime:
    if _frozen is not None:
        return _frozen + _offset
    real = datetime.now(SGT)
    if settings.as_of_date:
        d = date.fromisoformat(settings.as_of_date)
        real = datetime.combine(d, real.timetz())
    return real + _offset


def today() -> date:
    return now().date()


def freeze(at: datetime | str) -> None:
    """Pin the clock (tests/evals). Accepts ISO strings; naive values are read as SGT."""
    global _frozen, _offset
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    if at.tzinfo is None:
        at = at.replace(tzinfo=SGT)
    _frozen = at
    _offset = timedelta(0)


def advance(**kwargs: float) -> None:
    global _offset
    _offset += timedelta(**kwargs)


def reset() -> None:
    global _frozen, _offset
    _frozen = None
    _offset = timedelta(0)


def at(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=SGT)


def iso(dt: datetime) -> str:
    return dt.astimezone(SGT).isoformat(timespec="seconds")


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=SGT)
