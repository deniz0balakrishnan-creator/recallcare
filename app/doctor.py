"""Configuration check without revealing secrets:  python -m app.doctor [--ping]

--ping sends ONE tiny request to the configured LLM provider (a few tokens) to prove connectivity.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.settings import settings


def _set(v: str) -> str:
    return "set" if v else "MISSING"


def report() -> dict:
    s = settings
    return {
        "llm_provider": s.llm_provider,
        "gateway": {"url": s.gateway_url, "api_key": _set(s.gateway_api_key), "model": s.gateway_model,
                    "system_in_user": s.gateway_system_in_user, "max_request_bytes": s.llm_max_request_bytes},
        "openrouter": {"api_key": _set(s.openrouter_api_key), "model": s.openrouter_model},
        "channel": s.channel,
        "whatsapp": {"access_token": _set(s.wa_access_token), "phone_number_id": _set(s.wa_phone_number_id),
                     "graph_version": s.wa_graph_version or "MISSING", "app_secret": _set(s.wa_app_secret),
                     "verify_token": _set(s.wa_verify_token), "template": s.wa_template_name,
                     "template_approved": s.wa_template_approved,
                     "allowlist_count": len(s.allowlist), "demo_phone_map": sorted(s.phone_overrides)},
        "dashboard": {"user": s.dashboard_user, "password": _set(s.dashboard_password),
                      "session_secret": _set(s.session_secret)},
        "public_base_url": s.public_base_url or "(not set)",
    }


def ping(provider: str | None) -> dict:
    from app.llm import factory
    from app.llm.protocol import extract_json
    factory.set_provider(provider or (settings.llm_provider if settings.llm_provider != "mock" else "gateway"))
    llm = factory.get_llm()
    res = llm.complete("Reply with ONLY this JSON: {\"action\": \"pong\", \"args\": {}}",
                       [{"role": "user", "content": "ping"}], agent="doctor", max_tokens=30)
    try:
        parsed = extract_json(res.text)
    except Exception:
        parsed = None
    return {"provider": res.provider, "model": res.model, "fallback_used": res.fallback_used,
            "latency_ms": res.latency_ms, "tokens": [res.tokens_in, res.tokens_out],
            "tokens_estimated": res.tokens_estimated, "reply": res.text[:120], "json_ok": bool(parsed)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--provider", choices=["gateway", "openrouter"])
    a = ap.parse_args()
    print(json.dumps(report(), indent=2))
    if a.ping:
        try:
            print(json.dumps(ping(a.provider), indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"PING FAILED: {type(e).__name__}: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
