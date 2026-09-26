"""Simulator and Meta WhatsApp Cloud API adapters.

The simulator behaves exactly like WhatsApp: business-initiated messages must be templates, free text only inside
the 24-hour window that opens when the patient writes. Same allowlist, same validator.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx

from app.channels.base import InboundMessage, SendResult, WindowClosed, check_recipient
from app.settings import normalise_phone, settings, templates_config

log = logging.getLogger("recallcare.channel")


class SimulatorChannel:
    name = "simulator"

    def send_template(self, to: str, template_name: str, lang: str, params: list[str]) -> SendResult:
        check_recipient(to, self.name)
        if template_name not in templates_config()["templates"]:
            return SendResult(ok=False, error=f"unknown template {template_name}")
        return SendResult(ok=True, provider_id=f"sim-{uuid.uuid4().hex[:12]}")

    def send_text(self, to: str, text: str, *, window_open: bool) -> SendResult:
        check_recipient(to, self.name)
        if not window_open:
            raise WindowClosed("free text outside the 24h window (simulated WhatsApp error 131047)")
        return SendResult(ok=True, provider_id=f"sim-{uuid.uuid4().hex[:12]}")

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        return [InboundMessage(from_phone=normalise_phone(payload["from"]), text=str(payload.get("text", "")),
                               wa_message_id=payload.get("id"), channel=self.name)]


class WhatsAppChannel:
    """Meta WhatsApp Cloud API (Graph API version from env — never hard-coded)."""

    name = "whatsapp"

    def __init__(self) -> None:
        missing = [k for k, v in {"WHATSAPP_ACCESS_TOKEN": settings.wa_access_token,
                                  "WHATSAPP_PHONE_NUMBER_ID": settings.wa_phone_number_id,
                                  "WHATSAPP_GRAPH_VERSION": settings.wa_graph_version}.items() if not v]
        if missing:
            raise RuntimeError(f"WhatsApp channel not configured: missing {', '.join(missing)}")
        self.url = f"https://graph.facebook.com/{settings.wa_graph_version}/{settings.wa_phone_number_id}/messages"

    def _post(self, body: dict[str, Any]) -> SendResult:
        try:
            r = httpx.post(self.url, json=body, timeout=20,
                           headers={"Authorization": f"Bearer {settings.wa_access_token}"})
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"transport: {type(e).__name__}")
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and data.get("messages"):
            return SendResult(ok=True, provider_id=data["messages"][0].get("id"))
        err = (data.get("error") or {})
        if err.get("code") in (131047, 131026) or "re-engagement" in str(err.get("message", "")).lower():
            raise WindowClosed(f"WhatsApp {err.get('code')}: {err.get('message')}")
        return SendResult(ok=False, error=f"HTTP {r.status_code}: {str(err.get('message') or r.text)[:200]}")

    def send_template(self, to: str, template_name: str, lang: str, params: list[str]) -> SendResult:
        check_recipient(to, self.name)
        tpl = templates_config()["templates"][template_name]
        code = tpl["languages"].get(lang, tpl["languages"].get("en", {})).get("meta_language_code", "en_US")
        template: dict[str, Any] = {"name": template_name, "language": {"code": code}}
        if params:
            template["components"] = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._post({"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "template", "template": template})

    def send_text(self, to: str, text: str, *, window_open: bool) -> SendResult:
        check_recipient(to, self.name)
        if not window_open:
            raise WindowClosed("free text outside the 24h window")
        return self._post({"messaging_product": "whatsapp", "recipient_type": "individual", "to": to.lstrip("+"),
                           "type": "text", "text": {"preview_url": False, "body": text}})

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        return parse_wa_payload(payload)


def parse_wa_payload(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract inbound patient messages from a Meta webhook payload (status callbacks are ignored)."""
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []) or []:
                if m.get("type") == "text":
                    text = (m.get("text") or {}).get("body", "")
                elif m.get("type") == "button":
                    text = (m.get("button") or {}).get("text", "")
                else:
                    text = f"[non-text message: {m.get('type')}]"
                ts = datetime.fromtimestamp(int(m["timestamp"])) if m.get("timestamp") else None
                out.append(InboundMessage(from_phone=normalise_phone(m.get("from", "")), text=text,
                                          wa_message_id=m.get("id"), ts=ts, channel="whatsapp"))
    return out


def verify_signature(raw_body: bytes, header_value: str | None) -> bool:
    """Meta signs webhook bodies with HMAC-SHA256(app_secret) in X-Hub-Signature-256: 'sha256=<hex>'."""
    if not settings.wa_app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(settings.wa_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.split("=", 1)[1])


def get_adapter(name: str):
    if name == "whatsapp":
        return WhatsAppChannel()
    return SimulatorChannel()
