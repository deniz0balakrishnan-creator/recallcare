"""Hard token budget + call pacing for real LLM providers.

The organisers pause accounts that exceed their AWS usage limit, so spending is capped in code, not by habit.
Usage is kept in its own SQLite ledger (default data/llm_usage.db) so demo resets and eval runs — which use
throw-away app databases — are still counted. The mock provider is free and never recorded.
When a budget is exhausted the adapter refuses the call (non-retryable LLMError); agents then degrade to their
deterministic paths or escalate to staff — they never crash and never silently overspend.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app import clock
from app.llm.base import LLMError, LLMResult
from app.settings import settings

_lock = threading.Lock()
_last_call = 0.0


class BudgetExceeded(LLMError):
    def __init__(self, msg: str):
        super().__init__(msg, retryable=False)


def _db() -> sqlite3.Connection:
    p = settings.path(settings.llm_usage_db)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, day TEXT NOT NULL, ts TEXT NOT NULL,"
              " provider TEXT, model TEXT, agent TEXT, tokens_in INTEGER, tokens_out INTEGER, estimated INTEGER)")
    return c


def totals() -> dict[str, Any]:
    day = clock.today().isoformat()
    with _lock:
        c = _db()
        try:
            t_day = c.execute("SELECT COALESCE(SUM(tokens_in+tokens_out),0), COUNT(*) FROM usage WHERE day=?", (day,)).fetchone()
            t_all = c.execute("SELECT COALESCE(SUM(tokens_in+tokens_out),0), COUNT(*) FROM usage").fetchone()
        finally:
            c.close()
    return {"today": t_day[0], "calls_today": t_day[1], "total": t_all[0], "calls_total": t_all[1],
            "budget_daily": settings.llm_token_budget_daily, "budget_total": settings.llm_token_budget_total}


def check() -> None:
    t = totals()
    if settings.llm_token_budget_daily and t["today"] >= settings.llm_token_budget_daily:
        raise BudgetExceeded(f"daily token budget reached ({t['today']:,}/{settings.llm_token_budget_daily:,})")
    if settings.llm_token_budget_total and t["total"] >= settings.llm_token_budget_total:
        raise BudgetExceeded(f"total token budget reached ({t['total']:,}/{settings.llm_token_budget_total:,})")


def pace() -> None:
    """Minimum gap between real calls (the gateway answers bursts with 403)."""
    global _last_call
    gap = settings.llm_min_interval_ms / 1000.0
    with _lock:
        wait = _last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def record(res: LLMResult, agent: str) -> None:
    with _lock:
        c = _db()
        try:
            c.execute("INSERT INTO usage(day,ts,provider,model,agent,tokens_in,tokens_out,estimated) VALUES(?,?,?,?,?,?,?,?)",
                      (clock.today().isoformat(), clock.iso(clock.now()), res.provider, res.model, agent,
                       res.tokens_in, res.tokens_out, int(res.tokens_estimated)))
            c.commit()
        finally:
            c.close()


class BudgetedLLM:
    """Wraps any real provider: budget check → pacing → call → ledger."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name

    def complete(self, system: str, messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None, *,
                 agent: str = "", context: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResult:
        check()
        pace()
        res = self.inner.complete(system, messages, json_schema, agent=agent, context=context, max_tokens=max_tokens)
        record(res, agent)
        return res
