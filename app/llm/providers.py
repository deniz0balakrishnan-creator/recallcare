"""Real providers: the organisers' Bedrock-backed gateway (Ollama-native /api/chat) and OpenRouter."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.llm.base import LLMError, LLMResult, estimate_tokens, fit_request, payload_bytes
from app.settings import settings

log = logging.getLogger("recallcare.llm")

TEMPERATURE = 0.2


class GatewayLLM:
    """POST {LLM_GATEWAY_URL}/api/chat, X-API-Key auth."""

    name = "gateway"

    def __init__(self) -> None:
        if not settings.gateway_api_key:
            raise LLMError("LLM_GATEWAY_API_KEY not set", retryable=False)
        self.url = settings.gateway_url.rstrip("/") + "/api/chat"
        self.model = settings.gateway_model

    def _once(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> LLMResult:
        msgs = fit_request(system, messages, settings.llm_max_request_bytes)
        if settings.gateway_system_in_user:
            first = msgs[0]
            wire = [{"role": "user", "content": f"<rules>\n{system}\n</rules>\n\n{first['content']}"}] + msgs[1:]
        else:
            wire = [{"role": "system", "content": system}] + msgs
        body = {"model": self.model, "messages": wire, "stream": False,
                "options": {"num_predict": max_tokens, "temperature": TEMPERATURE}}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        t0 = time.monotonic()
        try:
            r = httpx.post(self.url, content=raw, timeout=settings.llm_timeout_s, headers={
                "Content-Type": "application/json", "X-API-Key": settings.gateway_api_key,
                "Authorization": f"Bearer {settings.gateway_api_key}"})
        except httpx.HTTPError as e:
            raise LLMError(f"gateway transport error: {type(e).__name__}") from e
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            text = r.text[:300].lower()
            quota = r.status_code == 429 or "quota" in text or "too many requests" in text
            raise LLMError(f"gateway HTTP {r.status_code}: {r.text[:200]}", retryable=not quota, status=r.status_code)
        data = r.json()
        content = (data.get("message") or {}).get("content") or ""
        tin, tout = data.get("prompt_eval_count"), data.get("eval_count")
        est = tin is None or tout is None
        return LLMResult(text=content, tokens_in=tin if tin is not None else estimate_tokens(system + json.dumps(msgs, ensure_ascii=False)),
                         tokens_out=tout if tout is not None else estimate_tokens(content), tokens_estimated=est,
                         model=self.model, provider=self.name, latency_ms=latency, request_bytes=len(raw))

    def complete(self, system: str, messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None, *,
                 agent: str = "", context: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResult:
        max_tokens = max_tokens or settings.llm_max_output_tokens
        last: LLMError | None = None
        for attempt in (1, 2):          # "errors or times out twice" → caller falls back
            try:
                return self._once(system, messages, max_tokens)
            except LLMError as e:
                last = e
                log.warning("gateway attempt %s failed: %s", attempt, e)
                if not e.retryable:
                    break
                if attempt == 1:
                    time.sleep(2.0)
        assert last is not None
        raise last


class OpenRouterLLM:
    """OpenAI-compatible chat completions on OpenRouter (organiser-approved fallback)."""

    name = "openrouter"

    def __init__(self) -> None:
        if not settings.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY not set", retryable=False)
        self.url = settings.openrouter_base_url.rstrip("/") + "/chat/completions"
        self.model = settings.openrouter_model

    def complete(self, system: str, messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None, *,
                 agent: str = "", context: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResult:
        msgs = fit_request(system, messages, settings.llm_max_request_bytes)
        body = {"model": self.model, "messages": [{"role": "system", "content": system}] + msgs,
                "max_tokens": max_tokens or settings.llm_max_output_tokens, "temperature": TEMPERATURE}
        t0 = time.monotonic()
        try:
            r = httpx.post(self.url, json=body, timeout=settings.llm_timeout_s, headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "X-Title": "RecallCare (NUS-ISS SMYA K2EZYJRZ)"})
        except httpx.HTTPError as e:
            raise LLMError(f"openrouter transport error: {type(e).__name__}") from e
        latency = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            raise LLMError(f"openrouter HTTP {r.status_code}: {r.text[:200]}", retryable=r.status_code >= 500,
                           status=r.status_code)
        data = r.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        est = "prompt_tokens" not in usage
        return LLMResult(text=content,
                         tokens_in=usage.get("prompt_tokens") or estimate_tokens(system + json.dumps(msgs, ensure_ascii=False)),
                         tokens_out=usage.get("completion_tokens") or estimate_tokens(content), tokens_estimated=est,
                         model=data.get("model") or self.model, provider=self.name, latency_ms=latency,
                         request_bytes=payload_bytes(system, msgs))


class FallbackLLM:
    """Gateway first; if it fails twice, one attempt on OpenRouter (if configured). Logged either way."""

    def __init__(self, primary: Any, secondary: Any | None) -> None:
        self.primary, self.secondary = primary, secondary
        self.name = primary.name

    def complete(self, system: str, messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None, *,
                 agent: str = "", context: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResult:
        try:
            return self.primary.complete(system, messages, json_schema, agent=agent, context=context, max_tokens=max_tokens)
        except LLMError as e:
            if self.secondary is None:
                raise
            log.warning("falling back to %s after %s error: %s", self.secondary.name, self.primary.name, e)
            res = self.secondary.complete(system, messages, json_schema, agent=agent, context=context, max_tokens=max_tokens)
            res.fallback_used = True
            return res
