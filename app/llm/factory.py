"""Pick the LLM provider from LLM_PROVIDER (mock | gateway | openrouter), with gateway→OpenRouter fallback."""
from __future__ import annotations

import logging

from app.llm.base import LLMError
from app.llm.mock import MockLLM
from app.llm.providers import FallbackLLM, GatewayLLM, OpenRouterLLM
from app.settings import settings

log = logging.getLogger("recallcare.llm")
_override: str | None = None


def set_provider(name: str | None) -> None:
    """Evals/tests switch provider at runtime (e.g. --mode mock)."""
    global _override
    _override = name


def provider_name() -> str:
    return _override or settings.llm_provider or "mock"


def get_llm():
    name = provider_name()
    if name == "mock":
        return MockLLM()
    if name == "openrouter":
        return OpenRouterLLM()
    if name == "gateway":
        try:
            secondary = OpenRouterLLM() if settings.openrouter_api_key else None
        except LLMError:
            secondary = None
        try:
            return FallbackLLM(GatewayLLM(), secondary)
        except LLMError as e:
            if secondary:
                log.warning("gateway unavailable (%s); using OpenRouter", e)
                return secondary
            raise
    raise LLMError(f"unknown LLM_PROVIDER {name}", retryable=False)
