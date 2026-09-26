"""Shared agent plumbing: prompt loading, untrusted-input wrapping, traced LLM calls through the JSON protocol."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app import trace
from app.llm import factory
from app.llm.base import LLMError
from app.llm.protocol import ProtocolOutcome, call_with_repair

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=16)
def _raw_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def prompt(name: str, **kw: Any) -> str:
    return _raw_prompt(name).format(**kw)


def wrap_untrusted(text: str) -> str:
    """Delimit patient text; neutralise any attempt to close the tag early."""
    safe = (text or "").replace("<patient_message>", "‹patient_message›").replace("</patient_message>", "‹/patient_message›")
    return f"<patient_message>\n{safe}\n</patient_message>"


def llm_step(agent: str, *, run_id: str, patient_id: int | None, system: str, user: str,
             allowed: dict[str, type[BaseModel] | None], context: dict[str, Any] | None = None,
             max_tokens: int | None = None) -> ProtocolOutcome:
    """One protocol exchange (≤1 repair). Every LLM call is traced with model/tokens/latency. Never raises."""
    try:
        llm = factory.get_llm()
        out = call_with_repair(llm, system, [{"role": "user", "content": user}], allowed, agent=agent,
                               context=context, max_tokens=max_tokens)
    except LLMError as e:
        trace.event(run_id, agent, "llm_call", outcome="error", patient_id=patient_id, args={"error": str(e)[:200]})
        return ProtocolOutcome(None, None, [], error=f"llm unavailable: {e}")
    for i, res in enumerate(out.calls):
        last = i == len(out.calls) - 1
        ok = out.ok if last else False
        trace.event(run_id, agent, "llm_call" if i == 0 else "llm_repair", patient_id=patient_id, llm=res,
                    outcome="ok" if ok else ("error" if last else "repaired"),
                    thought_summary=(out.action.thought_summary if (ok and out.action) else None),
                    args={"action": out.action.action if (ok and out.action) else None,
                          "request_bytes": res.request_bytes, "error": out.error if last else "invalid JSON → repair"})
    return out
