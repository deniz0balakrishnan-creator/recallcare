"""Hard token budget: refuse before calling, record every real call, degrade safely (organisers pause over-spenders)."""
import json

import httpx
import pytest

from app import db, service
from app.core import memory
from app.llm import budget, factory
from app.llm.base import LLMResult
from app.llm.budget import BudgetedLLM, BudgetExceeded
from app.llm.providers import GatewayLLM
from app.settings import settings


class FakeReal:
    name = "gateway"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, json_schema=None, *, agent="", context=None, max_tokens=None):
        self.calls += 1
        return LLMResult(text=json.dumps({"thought_summary": "t", "action": "verdict", "args": {
            "category": "safe", "confidence": 0.9, "urgency": "routine", "gloss_en": "", "summary_en": "ok"}}),
            tokens_in=1000, tokens_out=100, model="fake-sonnet", provider="gateway", latency_ms=5)


def test_budgeted_llm_records_and_refuses(monkeypatch):
    monkeypatch.setattr(settings, "llm_token_budget_daily", 2000)
    inner = FakeReal()
    llm = BudgetedLLM(inner)
    llm.complete("s", [{"role": "user", "content": "x"}], agent="guard")
    assert budget.totals()["today"] == 1100 and budget.totals()["calls_today"] == 1
    llm.complete("s", [{"role": "user", "content": "x"}], agent="guard")      # 2,200 ≥ 2,000 after this call
    with pytest.raises(BudgetExceeded):
        llm.complete("s", [{"role": "user", "content": "x"}], agent="guard")
    assert inner.calls == 2                                                     # refused BEFORE calling the provider


def test_ledger_uses_wall_clock_not_frozen_app_clock():
    """Evals freeze the app clock and the demo can pin AS_OF_DATE; real spend must land on the real day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import clock
    clock.freeze("2026-01-15T10:00:00+08:00")
    try:
        budget.record(LLMResult(text="", tokens_in=7, tokens_out=3, provider="gateway", model="m"), "guard")
    finally:
        clock.reset()
    real_day = datetime.now(ZoneInfo("Asia/Singapore")).date().isoformat()
    c = budget._db()
    try:
        days = [r[0] for r in c.execute("SELECT day FROM usage")]
    finally:
        c.close()
    assert days == [real_day] and budget.totals()["today"] == 10


def test_total_budget_also_enforced(monkeypatch):
    monkeypatch.setattr(settings, "llm_token_budget_daily", 10**9)
    monkeypatch.setattr(settings, "llm_token_budget_total", 500)
    llm = BudgetedLLM(FakeReal())
    llm.complete("s", [{"role": "user", "content": "x"}])
    with pytest.raises(BudgetExceeded):
        llm.complete("s", [{"role": "user", "content": "x"}])


def test_budget_exhausted_degrades_to_staff_not_crash(seeded, monkeypatch):
    r = db.q1("SELECT visit_type, due_date FROM recalls WHERE patient_id=5")
    memory.upsert(5, status="proposed", visit_type=r["visit_type"], due_date=r["due_date"], reason="t")
    service.approve([5])
    monkeypatch.setattr(settings, "llm_token_budget_daily", 1)
    budget.record(LLMResult(text="", tokens_in=5, tokens_out=0, provider="gateway", model="m"), "prior")
    fake = FakeReal()
    monkeypatch.setattr(factory, "provider_name", lambda: "gateway")
    monkeypatch.setattr(factory, "_real", lambda name: fake)
    out = service.handle_inbound(patient_id=5, text="What time do you open on Saturday?")
    assert fake.calls == 0
    assert db.q1("SELECT category FROM escalations WHERE patient_id=5")["category"] == "ai_unavailable"
    last = db.q1("SELECT body, status FROM messages WHERE patient_id=5 AND direction='out' ORDER BY id DESC LIMIT 1")
    assert last["status"] == "sent" and "clinic team" in last["body"]            # holding reply, no model text
    # deterministic paths keep working with the model switched off
    out2 = service.handle_inbound(patient_id=6, text="STOP")
    assert out2["verdict"] == "opt_out"


def test_gateway_openai_style(monkeypatch):
    monkeypatch.setattr(settings, "gateway_api_key", "k")
    monkeypatch.setattr(settings, "gateway_url", "https://gw.example")
    monkeypatch.setattr(settings, "gateway_api_style", "openai")
    seen = {}

    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}

    monkeypatch.setattr(httpx, "post", lambda url, content=None, **kw: seen.update(url=url, body=json.loads(content)) or R())
    res = GatewayLLM().complete("S", [{"role": "user", "content": "u"}], max_tokens=40)
    assert seen["url"] == "https://gw.example/v1/chat/completions"
    assert seen["body"]["max_tokens"] == 40 and "options" not in seen["body"]
    assert (res.text, res.tokens_in, res.tokens_out) == ("hi", 7, 2)
