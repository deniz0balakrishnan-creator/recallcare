"""Real providers: the organisers' Bedrock-backed gateway (Ollama / OpenAI-compatible / Bedrock Converse JSON) and OpenRouter."""
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


def resolve_gateway_style(url: str, configured: str) -> str:
    """`auto` picks the wire format from the gateway URL (see the technical document, §3.1)."""
    if configured in ("ollama", "openai", "converse"):
        return configured
    u = url.lower().rstrip("/")
    if "execute-api" in u:
        return "converse"          # API Gateway → Lambda → Bedrock Converse
    if u.endswith("/v1") or u.endswith("/chat/completions"):
        return "openai"
    return "ollama"                # starter-kit default: Ollama-compatible /api/chat


def _native_tool_call_to_json(name: str, args: Any) -> str:
    """If a model answers with a native tool call anyway, express it in our JSON protocol (still validated)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return json.dumps({"thought_summary": "(native tool call)", "action": name, "args": args or {}}, ensure_ascii=False)


class GatewayLLM:
    """Organisers' Bedrock-backed gateway, three documented wire formats (LLM_GATEWAY_API_STYLE, default auto):
    ollama  → POST {url}/api/chat                   (Ollama-compatible proxy, current starter kit)
    openai  → POST {url}/v1/chat/completions        (OpenAI-compatible, starter-kit Copilot section)
    converse→ POST {url} as given                   (API Gateway → Lambda → Bedrock Converse JSON)
    Auth is X-API-Key on all of them. We never send native `tools`: agents use our validated JSON protocol."""

    name = "gateway"

    def __init__(self) -> None:
        if not settings.gateway_api_key:
            raise LLMError("LLM_GATEWAY_API_KEY not set", retryable=False)
        base = settings.gateway_url.rstrip("/")
        self.style = resolve_gateway_style(base, settings.gateway_api_style)
        if self.style == "openai":
            self.url = base if base.endswith("/chat/completions") else base + (
                "/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        elif self.style == "converse":
            self.url = base
        else:
            self.url = base if base.endswith("/api/chat") else base + "/api/chat"
        self.model = settings.gateway_model

    def _once(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> LLMResult:
        msgs = fit_request(system, messages, settings.llm_max_request_bytes)
        if settings.gateway_system_in_user:
            first = msgs[0]
            msgs = [{"role": "user", "content": f"<rules>\n{system}\n</rules>\n\n{first['content']}"}] + msgs[1:]
            wire = msgs
        else:
            wire = [{"role": "system", "content": system}] + msgs
        if self.style == "converse":
            body = {"modelId": self.model,
                    "messages": [{"role": m["role"], "content": [{"text": m["content"]}]} for m in msgs],
                    "inferenceConfig": {"temperature": TEMPERATURE, "maxTokens": max_tokens}}
            if not settings.gateway_system_in_user:
                body["system"] = [{"text": system}]
        elif self.style == "openai":
            body = {"model": self.model, "messages": wire, "stream": False, "max_tokens": max_tokens,
                    "temperature": TEMPERATURE}
        else:
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
        try:
            data = r.json()
        except ValueError as e:
            raise LLMError(f"gateway returned non-JSON: {r.text[:120]}") from e
        if isinstance(data, dict) and isinstance(data.get("body"), str):     # Lambda proxy envelope, if passed through
            try:
                data = json.loads(data["body"])
            except json.JSONDecodeError:
                pass
        if self.style == "converse":
            blocks = ((data.get("output") or {}).get("message") or {}).get("content") or []
            texts = [b["text"] for b in blocks if isinstance(b, dict) and "text" in b]
            tool = next((b["toolUse"] for b in blocks if isinstance(b, dict) and "toolUse" in b), None)
            content = "\n".join(texts) if texts else (_native_tool_call_to_json(tool.get("name", ""), tool.get("input"))
                                                     if tool else "")
            usage = data.get("usage") or {}
            tin, tout = usage.get("inputTokens"), usage.get("outputTokens")
        elif self.style == "openai":
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            if not content and msg.get("tool_calls"):
                fn = msg["tool_calls"][0].get("function", {})
                content = _native_tool_call_to_json(fn.get("name", ""), fn.get("arguments"))
            usage = data.get("usage") or {}
            tin, tout = usage.get("prompt_tokens"), usage.get("completion_tokens")
        else:
            msg = data.get("message") or {}
            content = msg.get("content") or ""
            if not content and msg.get("tool_calls"):
                fn = msg["tool_calls"][0].get("function", {})
                content = _native_tool_call_to_json(fn.get("name", ""), fn.get("arguments"))
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
