"""Channel adapter interface. Both implementations enforce the allowlist themselves (defence in depth)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.validator import recipient_allowed


class InboundMessage(BaseModel):
    from_phone: str
    text: str
    wa_message_id: str | None = None
    ts: datetime | None = None
    channel: str


class SendResult(BaseModel):
    ok: bool
    provider_id: str | None = None
    error: str | None = None


class ChannelError(Exception):
    pass


class RecipientNotAllowed(ChannelError):
    pass


class WindowClosed(ChannelError):
    """Free text outside the 24h customer-service window — must use a template."""


class ChannelAdapter(Protocol):
    name: str

    def send_template(self, to: str, template_name: str, lang: str, params: list[str]) -> SendResult: ...

    def send_text(self, to: str, text: str, *, window_open: bool) -> SendResult: ...

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]: ...


def check_recipient(to: str, channel: str) -> None:
    if not recipient_allowed(to, channel):
        raise RecipientNotAllowed(f"{channel}: recipient not on allowlist")
