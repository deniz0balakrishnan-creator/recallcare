"""LLM adapter interface: complete(system, messages, json_schema=None) -> LLMResult."""
from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel


class LLMResult(BaseModel):
    text: str
    parsed: dict[str, Any] | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_estimated: bool = False
    model: str = ""
    provider: str = ""
    latency_ms: int = 0
    fallback_used: bool = False
    request_bytes: int = 0


class LLMError(Exception):
    """Provider failure (network, auth, quota, 5xx, WAF 403…)."""

    def __init__(self, msg: str, *, retryable: bool = True, status: int | None = None):
        super().__init__(msg)
        self.retryable = retryable
        self.status = status


class LLMRequestTooLarge(LLMError):
    def __init__(self, size: int, limit: int):
        super().__init__(f"request {size} bytes exceeds limit {limit}", retryable=False)


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, str]], json_schema: dict[str, Any] | None = None, *,
                 agent: str = "", context: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResult:
        """`context` is structured data only the mock provider reads (real providers ignore it)."""
        ...


def estimate_tokens(text: str) -> int:
    # ~4 bytes/token is a conservative estimate across scripts (CJK/Tamil use 3 bytes per char in UTF-8).
    return max(1, len(text.encode("utf-8")) // 4)


def payload_bytes(system: str, messages: list[dict[str, str]]) -> int:
    return len(json.dumps({"system": system, "messages": messages}, ensure_ascii=False).encode("utf-8")) + 200


def fit_request(system: str, messages: list[dict[str, str]], max_bytes: int) -> list[dict[str, str]]:
    """Drop the oldest turns until the request fits under the gateway's WAF limit. Never drops the last turn."""
    msgs = list(messages)
    while payload_bytes(system, msgs) > max_bytes and len(msgs) > 1:
        msgs.pop(0)
        while msgs and msgs[0]["role"] != "user" and len(msgs) > 1:   # keep user-first ordering
            msgs.pop(0)
    size = payload_bytes(system, msgs)
    if size > max_bytes:
        raise LLMRequestTooLarge(size, max_bytes)
    return msgs
