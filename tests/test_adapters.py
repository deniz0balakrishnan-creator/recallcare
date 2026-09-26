"""Real adapters against faked HTTP: request shapes, auth headers, retry/fallback, WhatsApp error mapping.
(De-risks Phase 3 before live credentials exist.)"""
import json

import httpx
import pytest

from app.channels.adapters import WhatsAppChannel
from app.channels.base import RecipientNotAllowed, WindowClosed
from app.llm.base import LLMError
from app.llm.providers import FallbackLLM, GatewayLLM, OpenRouterLLM
from app.settings import settings


class FakeResp:
    def __init__(self, status: int, body: dict | str, ctype: str = "application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": ctype}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body if isinstance(self._body, dict) else json.loads(self._body)


@pytest.fixture
def gw(monkeypatch):
    monkeypatch.setattr(settings, "gateway_api_key", "gw-test-key")
    monkeypatch.setattr(settings, "gateway_url", "https://gw.example")
    monkeypatch.setattr("app.llm.providers.time.sleep", lambda s: None)
    return monkeypatch


def test_gateway_request_shape_and_tokens(gw):
    seen = {}

    def fake_post(url, content=None, timeout=None, headers=None, **kw):
        seen.update(url=url, headers=headers, body=json.loads(content))
        return FakeResp(200, {"message": {"role": "assistant", "content": '{"action":"x","args":{}}'},
                              "prompt_eval_count": 111, "eval_count": 22, "done_reason": "stop"})

    gw.setattr(httpx, "post", fake_post)
    res = GatewayLLM().complete("SYS", [{"role": "user", "content": "hi"}], max_tokens=50)
    assert seen["url"] == "https://gw.example/api/chat"
    assert seen["headers"]["X-API-Key"] == "gw-test-key"
    assert seen["body"]["stream"] is False and seen["body"]["options"]["num_predict"] == 50
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert (res.tokens_in, res.tokens_out, res.tokens_estimated) == (111, 22, False)


def test_gateway_system_in_user_mode(gw):
    gw.setattr(settings, "gateway_system_in_user", True)
    seen = {}
    gw.setattr(httpx, "post", lambda url, content=None, **kw: seen.update(body=json.loads(content)) or
               FakeResp(200, {"message": {"content": "ok"}}))
    res = GatewayLLM().complete("RULES", [{"role": "user", "content": "hi"}])
    assert seen["body"]["messages"][0]["role"] == "user" and "RULES" in seen["body"]["messages"][0]["content"]
    assert res.tokens_estimated  # counts missing → estimated and flagged


def test_gateway_retries_then_falls_back_to_openrouter(gw):
    gw.setattr(settings, "openrouter_api_key", "or-test-key")
    calls = []

    def fake_post(url, content=None, json=None, timeout=None, headers=None, **kw):
        calls.append(url)
        if "gw.example" in url:
            return FakeResp(403, "<html>WAF</html>", "text/html")
        return FakeResp(200, {"choices": [{"message": {"content": "fallback ok"}}], "model": "anthropic/claude-haiku-4.5",
                              "usage": {"prompt_tokens": 9, "completion_tokens": 3}})

    gw.setattr(httpx, "post", fake_post)
    res = FallbackLLM(GatewayLLM(), OpenRouterLLM()).complete("s", [{"role": "user", "content": "hi"}])
    assert [c for c in calls if "gw.example" in c] == ["https://gw.example/api/chat"] * 2   # retried once
    assert res.fallback_used and res.provider == "openrouter" and res.text == "fallback ok"


def test_gateway_quota_error_is_not_retried(gw):
    calls = []
    gw.setattr(httpx, "post", lambda url, **kw: calls.append(url) or FakeResp(429, {"error": "token quota exceeded"}))
    with pytest.raises(LLMError):
        GatewayLLM().complete("s", [{"role": "user", "content": "hi"}])
    assert len(calls) == 1


def test_gateway_refuses_oversized_request_without_calling(gw):
    gw.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not call the gateway"))
    with pytest.raises(LLMError):
        GatewayLLM().complete("x" * 9000, [{"role": "user", "content": "hi"}])


@pytest.fixture
def wa(monkeypatch):
    monkeypatch.setattr(settings, "wa_access_token", "EAA-test")
    monkeypatch.setattr(settings, "wa_phone_number_id", "123456")
    monkeypatch.setattr(settings, "wa_graph_version", "v99.0")
    monkeypatch.setattr(settings, "whatsapp_allowlist", "+6590000001")
    return monkeypatch


def test_whatsapp_template_payload(wa):
    seen = {}
    wa.setattr(httpx, "post", lambda url, json=None, timeout=None, headers=None: seen.update(url=url, body=json, h=headers)
               or FakeResp(200, {"messages": [{"id": "wamid.X"}]}))
    res = WhatsAppChannel().send_template("+6590000001", "recall_reminder", "zh", ["陈女士", "Sunbird Family Dental", "例行口腔检查"])
    assert res.ok and res.provider_id == "wamid.X"
    assert seen["url"] == "https://graph.facebook.com/v99.0/123456/messages"
    assert seen["h"]["Authorization"] == "Bearer EAA-test"
    t = seen["body"]["template"]
    assert seen["body"]["to"] == "6590000001" and t["language"]["code"] == "zh_CN"
    assert [p["text"] for p in t["components"][0]["parameters"]][0] == "陈女士"


def test_whatsapp_blocks_non_allowlisted_before_any_http(wa):
    wa.setattr(httpx, "post", lambda *a, **k: pytest.fail("must not call Meta"))
    with pytest.raises(RecipientNotAllowed):
        WhatsAppChannel().send_text("+6591234567", "hi", window_open=True)
    with pytest.raises(RecipientNotAllowed):                          # synthetic numbers never go to the real channel
        WhatsAppChannel().send_text("+6555500001", "hi", window_open=True)


def test_whatsapp_reengagement_error_maps_to_window_closed(wa):
    wa.setattr(httpx, "post", lambda *a, **k: FakeResp(400, {"error": {"code": 131047, "message": "Re-engagement message"}}))
    with pytest.raises(WindowClosed):
        WhatsAppChannel().send_text("+6590000001", "hi", window_open=True)
