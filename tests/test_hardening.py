"""Edge cases found while preparing for the live run: stale offers, restarts, concurrency, bursts, odd inputs."""
from concurrent.futures import ThreadPoolExecutor

from app import clock, db, graph, service
from app.core import memory
from app.settings import settings


def _contact(pid):
    r = db.q1("SELECT visit_type, due_date FROM recalls WHERE patient_id=?", (pid,))
    memory.upsert(pid, status="proposed", visit_type=r["visit_type"], due_date=r["due_date"], reason="test")
    service.approve([pid], staff="test")


def _last_out(pid):
    return db.q1("SELECT body, status FROM messages WHERE patient_id=? AND direction='out' ORDER BY id DESC LIMIT 1", (pid,))


def test_stale_offer_is_replaced_not_booked_in_the_past(seeded):
    _contact(5)
    service.handle_inbound(patient_id=5, text="Can I come next Tuesday morning?")      # offers Tue 29 Sep
    clock.advance(days=5)                                                              # now Thu 1 Oct: offers expired
    out = service.handle_inbound(patient_id=5, text="1")
    assert not db.q1("SELECT 1 FROM appointments WHERE patient_id=5 AND status='booked' AND start_ts < ?",
                     (clock.iso(clock.now()),))
    assert out["outcome"].startswith("options_expired")
    assert "passed" in _last_out(5)["body"]
    out2 = service.handle_inbound(patient_id=5, text="1")                              # picks from the fresh offer
    assert out2["outcome"].startswith("booked")


def test_conversation_survives_a_server_restart(seeded):
    _contact(5)
    service.handle_inbound(patient_id=5, text="Can I come next Tuesday morning?")
    path = str(settings.path(settings.checkpoint_db_path))
    g = graph._graphs.pop(path)                  # simulate a process restart: drop the compiled graph + connection
    g.checkpointer.conn.close()
    out = service.handle_inbound(patient_id=5, text="2")
    assert out["outcome"].startswith("booked")   # offered slots came back from the SQLite checkpointer


def test_concurrent_messages_from_different_patients(seeded):
    for pid in (2, 3, 4, 5, 6):
        _contact(pid)
    texts = {2: "Boleh saya datang hari Sabtu pagi?", 3: "நாளை மாலை வரலாமா?", 4: "What time do you open?",
             5: "Can I come next Tuesday morning?", 6: "STOP"}
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda kv: service.handle_inbound(patient_id=kv[0], text=kv[1]), texts.items()))
    assert all(r["status"] == "processed" for r in results), results
    assert db.q1("SELECT opted_out FROM patients WHERE id=6")["opted_out"] == 1


def test_message_burst_is_rate_limited(seeded, monkeypatch):
    monkeypatch.setattr(settings, "inbound_rate_per_min", 5)
    _contact(5)
    statuses = [service.handle_inbound(patient_id=5, text="hi")["status"] for _ in range(8)]
    assert statuses.count("processed") == 5 and statuses.count("rate_limited") == 3


def test_non_text_whatsapp_message_gets_a_safe_reply(seeded):
    _contact(5)
    out = service.handle_inbound(patient_id=5, text="[non-text message: image]")
    assert out["status"] == "processed"
    assert _last_out(5)["status"] == "sent"


def test_reply_after_booking_does_not_double_book(seeded):
    _contact(5)
    service.handle_inbound(patient_id=5, text="Can I come next Tuesday morning?")
    service.handle_inbound(patient_id=5, text="1")
    service.handle_inbound(patient_id=5, text="1")          # patient sends "1" again
    n = db.q1("SELECT COUNT(*) n FROM appointments WHERE patient_id=5 AND status='booked'")["n"]
    assert n == 1


def test_offer_stays_bookable_for_the_reply_window(seeded):
    """Offers start >= 3 h out and stay bookable until 1 h before: a patient who replies within 2 h can book."""
    _contact(5)
    service.handle_inbound(patient_id=5, text="yes")          # earliest options, possibly today
    clock.advance(hours=1, minutes=55)
    out = service.handle_inbound(patient_id=5, text="1")
    assert out["outcome"].startswith("booked"), out
