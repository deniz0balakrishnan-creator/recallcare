"""Web layer: login wall, webhook verification + signature, simulator round-trip, health."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.settings import settings


@pytest.fixture
def client(seeded, monkeypatch):
    monkeypatch.setattr(settings, "auto_jobs", False)
    import app.main as m
    monkeypatch.setattr(m, "_dashboard_password", "pw-test")
    with TestClient(m.app) as c:
        yield c


def _login(c):
    r = c.post("/login", data={"username": settings.dashboard_user, "password": "pw-test"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_dashboard_requires_login(client):
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.post("/api/triage/run").status_code == 401
    r = client.post("/login", data={"username": "staff", "password": "wrong"}, follow_redirects=False)
    assert "error" in r.headers["location"]


def test_pages_render_after_login(client):
    _login(client)
    client.post("/api/triage/run")
    for path in ["/", "/conversations", "/trace", "/impact", "/evals", "/simulator?embed=1"]:
        assert client.get(path).status_code == 200, path


def test_healthz_is_public_and_minimal(client):
    j = client.get("/healthz").json()
    assert j["status"] == "ok" and j["synthetic_patients"] == 72
    assert "password" not in json.dumps(j).lower()


def test_webhook_verify_challenge(client, monkeypatch):
    monkeypatch.setattr(settings, "wa_verify_token", "vt-123")
    ok = client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "vt-123", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert bad.status_code == 403


def _wa_payload(phone: str, text: str, mid: str) -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": phone.lstrip("+"), "id": mid, "timestamp": "1790000000", "type": "text", "text": {"body": text}}]}}]}]}


def test_webhook_rejects_unsigned_and_accepts_signed(client, monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", "app-secret")
    body = json.dumps(_wa_payload("+6555500005", "What time do you open?", "wamid.1")).encode()
    assert client.post("/webhook/whatsapp", content=body).status_code == 401
    assert client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.json()["received"] == 1
    assert db.q1("SELECT body FROM messages WHERE wa_message_id='wamid.1'")["body"] == "What time do you open?"
    # Meta retries: the same message id is processed once
    client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sig})
    assert db.q1("SELECT COUNT(*) n FROM messages WHERE wa_message_id='wamid.1'")["n"] == 1


def test_unknown_sender_gets_no_reply(client, monkeypatch):
    monkeypatch.setattr(settings, "wa_app_secret", "app-secret")
    body = json.dumps(_wa_payload("+6591234567", "hi", "wamid.2")).encode()
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    client.post("/webhook/whatsapp", content=body, headers={"X-Hub-Signature-256": sig})
    assert db.q1("SELECT COUNT(*) n FROM messages WHERE direction='out'")["n"] == 0


def test_simulator_round_trip(client):
    _login(client)
    client.post("/api/triage/run")
    client.post("/api/batch/approve", json={"patient_ids": [1]})
    r = client.post("/api/simulator/1/send", json={"text": "下星期二上午可以吗"}).json()
    assert r["ok"] and r["result"]["outcome"].startswith("offer")
    msgs = client.get("/api/simulator/1/messages").json()["messages"]
    assert msgs[-1]["body"].startswith("以下是可预约的时间")


def test_judge_account_is_separate_and_traced(client, monkeypatch):
    monkeypatch.setattr(settings, "judge_password", "judge-pw")
    r = client.post("/login", data={"username": "judge", "password": "pw-test"}, follow_redirects=False)
    assert "error" in r.headers["location"]                     # staff password doesn't open the judge account
    r = client.post("/login", data={"username": "judge", "password": "judge-pw"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    client.post("/api/triage/run")
    client.post("/api/batch/approve", json={"patient_ids": [1]})
    by = db.q1("SELECT args FROM trace_events WHERE action='approve_outreach'")["args"]
    assert '"by": "judge"' in by


def test_no_judge_account_unless_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "judge_password", "")
    r = client.post("/login", data={"username": "judge", "password": ""}, follow_redirects=False)
    assert "error" in r.headers["location"]
