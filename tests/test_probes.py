"""Fixes from the judge-question probes: identity/scam/visit-type questions are answered, not escalated;
hand-over keywords match requests only; holding replies fit what actually happened."""
from typing import get_args

import pytest

from app import db, service
from app.agents.guard import _holding_for
from app.core import memory, rules
from app.core.validator import _ADVICE
from app.settings import clinic_config
from app.tools.schemas import InfoTopic


@pytest.mark.parametrize("text", ["Am I talking to a real person or a bot?", "Are you a human?", "你是真人吗？", "你是人工智能吗？",
                                  "Adakah saya bercakap dengan orang sebenar?", "நான் மனிதரிடம் பேசுகிறேனா?"])
def test_identity_questions_are_not_handover_requests(text):
    assert "human_request" not in rules.prescreen(text).hits


@pytest.mark.parametrize("text", ["This is useless. Let me talk to a real person now.", "Please call me", "我要转人工",
                                  "Saya mahu bercakap dengan staf", "நான் மனிதரிடம் பேச வேண்டும்"])
def test_real_handover_requests_still_escalate(text):
    assert "human_request" in rules.prescreen(text).hits


def test_holding_reply_matches_the_situation():
    assert _holding_for("clinical", "routine", "What is a gum care review?") == "holding_question"
    assert _holding_for("clinical", "soon", "My gum has been bleeding for weeks") == "holding_clinical"
    assert _holding_for("clinical", "routine", "my tooth hurts") == "holding_clinical"      # a symptom word keeps the 995 text
    assert _holding_for("clinical", "urgent", "") == "holding_clinical"
    assert _holding_for("human_request", "routine", "talk to a person") == "holding_human"
    assert _holding_for("security", "soon", "") == "holding_security"


@pytest.mark.parametrize("path", ["config/clinics/dental.yaml", "config/clinics/gp.yaml"])
def test_every_info_topic_exists_and_passes_the_advice_filter(path):
    sheet = clinic_config(path)["info_sheet"]
    for topic in get_args(InfoTopic):
        assert topic in sheet, f"{path} lacks info topic {topic}"
        assert not any(p.search(sheet[topic]) for p in _ADVICE), f"{topic} trips the clinical-advice filter"


def test_what_is_this_service_is_answered_not_escalated(seeded):
    r = db.q1("SELECT visit_type, due_date FROM recalls WHERE patient_id=4")
    memory.upsert(4, status="proposed", visit_type=r["visit_type"], due_date=r["due_date"], reason="t")
    service.approve([4], staff="test")
    service.handle_inbound(patient_id=4, text="What is this service?")
    out = db.q1("SELECT body FROM messages WHERE patient_id=4 AND direction='out' ORDER BY id DESC LIMIT 1")["body"]
    assert "automated assistant" in out and "995" not in out
    assert not db.q("SELECT id FROM escalations WHERE patient_id=4 AND status='open'")
