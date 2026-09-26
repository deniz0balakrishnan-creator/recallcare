"""Deterministic mock LLM. Returns the same JSON shapes a real model must return, driven by rule helpers.

Used for tests, CI, the zero-credential demo and mock-mode evals. It reads the structured `context` the agents pass
(real providers ignore it). `MockLLM.fail_next` lets tests exercise the repair/escalation path.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core import rules
from app.core.lang import render, render_pair
from app.llm.base import LLMResult, estimate_tokens
from app.settings import clinic_config

_I18N = Path(__file__).with_name("mock_i18n.yaml")


@lru_cache(maxsize=1)
def _i18n() -> dict[str, dict[str, str]]:
    return yaml.safe_load(_I18N.read_text(encoding="utf-8"))


def info_answer(topic: str, lang: str) -> tuple[str, str]:
    en = clinic_config()["info_sheet"][topic]
    return (en if lang == "en" else _i18n().get(topic, {}).get(lang, en)), en


class MockLLM:
    name = "mock"
    fail_next = 0            # number of upcoming calls that return unparseable text

    def complete(self, system: str, messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None, *,
                 agent: str = "", context: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResult:
        ctx = context or {}
        if MockLLM.fail_next > 0:
            MockLLM.fail_next -= 1
            text = "Sure! I'd be happy to help with that."          # prose, no JSON → protocol error
        else:
            handler = getattr(self, f"_{agent}", None)
            obj = handler(ctx) if handler else {"thought_summary": "no mock handler", "action": "escalate",
                                                "args": {"category": "other", "urgency": "routine",
                                                         "summary_en": f"mock has no handler for {agent}"}}
            text = json.dumps(obj, ensure_ascii=False)
        prompt = system + json.dumps(messages, ensure_ascii=False)
        return LLMResult(text=text, tokens_in=estimate_tokens(prompt), tokens_out=estimate_tokens(text),
                         tokens_estimated=True, model="mock-deterministic", provider="mock", latency_ms=1,
                         request_bytes=len(prompt.encode()))

    # ------------------------------------------------------------ triage: reasons only
    def _triage(self, ctx: dict[str, Any]) -> dict[str, Any]:
        items = ctx.get("items", [])
        return {"thought_summary": f"ranked {len(items)} patients by deterministic score; wrote staff reasons",
                "action": "propose_batch",
                "args": {"patient_ids": [i["patient_id"] for i in items],
                         "reasons": {str(i["patient_id"]): "; ".join(i["facts"])[:180] for i in items}}}

    # ------------------------------------------------------------ guard: classify
    def _guard(self, ctx: dict[str, Any]) -> dict[str, Any]:
        ps = rules.prescreen(ctx.get("text", ""))
        text, lang = ctx.get("text", ""), ctx.get("lang", "en")
        cat_map = {"injection": "injection", "impersonation": "impersonation", "other_recipient": "other_recipient",
                   "clinical": "clinical", "complaint": "complaint", "billing": "billing",
                   "human_request": "human_request", "abuse": "abuse", "opt_out": "opt_out"}
        cat = cat_map.get(ps.primary or "", "safe")
        if cat == "safe" and ps.emoji_only:
            cat = "unclear"
        urgency = "urgent" if cat == "clinical" and rules.has_any(text, ["swollen", "swelling", "肿", "bengkak", "வீக்கம்", "fever", "发烧", "demam"]) else (
            "soon" if cat in {"clinical", "complaint", "injection", "impersonation"} else "routine")
        gloss = text if lang == "en" else f"(mock mode: untranslated) {text}"
        return {"thought_summary": f"prescreen={ps.primary or 'none'}; classified {cat}", "action": "verdict",
                "args": {"category": cat, "confidence": 0.95 if cat != "unclear" else 0.5, "urgency": urgency,
                         "gloss_en": gloss[:300], "summary_en": f"Patient message classified {cat}."[:300]}}

    # ------------------------------------------------------------ conversation: decide next action
    def _conversation(self, ctx: dict[str, Any]) -> dict[str, Any]:
        text, lang = ctx.get("text", ""), ctx.get("lang", "en")
        visit = ctx.get("visit_label", "check-up")
        tool_results = ctx.get("tool_results") or []
        short = ctx.get("short_reply")
        pending = ctx.get("pending_options") or []

        def respond(key: str, set_status: str | None = None, **kw: str) -> dict[str, Any]:
            t, g = render_pair(key, lang, **kw)
            args: dict[str, Any] = {"text": t, "gloss_en": g}
            if set_status:
                args["set_status"] = set_status
            return {"thought_summary": f"reply with {key}", "action": "respond", "args": args}

        if tool_results:
            last = tool_results[-1]
            if last.get("tool") == "get_clinic_info":
                t, g = info_answer(last["args"]["topic"], lang)
                follow = render("ask_when", lang, visit=visit) if not ctx.get("has_booking") else ""
                return {"thought_summary": f"answer {last['args']['topic']} from the info sheet",
                        "action": "respond", "args": {"text": (t + ("\n\n" + follow if follow else ""))[:1000],
                                                      "gloss_en": g}}
        if short and short.startswith("option:") and pending:
            return {"thought_summary": "patient picked an offered option", "action": "request_scheduling",
                    "args": {"intent": "choose_option", "option_number": int(short.split(":")[1])}}
        if short == "cancel" or (rules.has_any(text, rules.CANCEL_WORDS) and ctx.get("has_booking")):
            return {"thought_summary": "patient wants to cancel", "action": "request_scheduling",
                    "args": {"intent": "cancel", "preference_text": text[:300]}}
        if rules.has_any(text, rules.LIMIT_WORDS):
            return respond("booking_limit")
        if short == "change" or (ctx.get("has_booking") and rules.has_any(text, rules.RESCHEDULE_WORDS)):
            return {"thought_summary": "patient wants a different time", "action": "request_scheduling",
                    "args": {"intent": "reschedule", "preference_text": text[:300]}}
        topic = rules.detect_info_topic(text)
        if topic and not short:
            return {"thought_summary": f"logistics question about {topic}", "action": "get_clinic_info",
                    "args": {"topic": topic}}
        if short == "no" or rules.has_any(text, rules.DECLINE_WORDS):
            return respond("decline_ack", set_status="declined")
        if short == "thanks":
            return respond("thanks_ack")
        if rules.mentions_time(text) or rules.has_any(text, rules.BOOKING_WORDS):
            return {"thought_summary": "patient wants to book; hand off to scheduling", "action": "request_scheduling",
                    "args": {"intent": "find_times", "preference_text": text[:300]}}
        if short in {"yes", "greeting"}:
            return respond("ask_when", visit=visit)
        return respond("clarify", visit=visit)

    # ------------------------------------------------------------ scheduling: read the time preference
    def _scheduling(self, ctx: dict[str, Any]) -> dict[str, Any]:
        pref = rules.parse_time_preference(ctx.get("preference_text", ""))
        pref.pop("_matched", None)
        pref["duration_min"] = ctx.get("duration_min", 30)
        return {"thought_summary": "parsed day/part-of-day from the patient's words", "action": "find_slots",
                "args": pref}
