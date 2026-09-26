"""'Run demo scenario': reset the synthetic clinic and replay a repeatable storyline.

Yesterday (scripted, deterministic — zero model calls): a batch without Mdm Tan is approved and reminded; some
patients reply "yes"/"1" and book, one declines, one opts out. Heroes 2–5 are left "contacted" so the video can
show them replying live. Today: triage runs and proposes a fresh batch that includes Mdm Tan (18 months overdue),
Mdm Lim (unfinished treatment) and Mr Murugan (sensitive → Tier 2).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app import clock, db, graph, service
from app.core import memory, recall
from app.seed import seed
from app.settings import settings

HEROES_CONTACTED_YESTERDAY = [2, 3, 4, 5, 6]
TODAY_HEROES = {1, 7, 8}


def _reset_checkpoints() -> None:
    path = str(settings.path(settings.checkpoint_db_path))
    with graph._lock:
        g = graph._graphs.pop(path, None)
        if g is not None:
            try:
                g.checkpointer.conn.close()
            except Exception:
                pass
    for suffix in ("", "-wal", "-shm"):
        f = Path(path + suffix)
        if f.exists():
            f.unlink()


def reset_and_play(storyline: bool = True) -> dict[str, Any]:
    with graph.RUN_LOCK:
        _reset_checkpoints()
        seed()
        service._rate.clear()
    if not storyline:
        return {"status": "reset"}
    channel = db.get_setting("channel")
    db.set_setting("channel", "simulator")          # yesterday's scripted replies never touch real phones
    saved_offset = clock._offset
    clock.advance(days=-1)
    try:
        due = [d for d in recall.list_due(limit=200) if d.patient_id not in TODAY_HEROES]
        crowd = [d for d in due if d.patient_id not in HEROES_CONTACTED_YESTERDAY][:7]
        batch = [d for d in due if d.patient_id in HEROES_CONTACTED_YESTERDAY] + crowd
        for d in batch:
            memory.upsert(d.patient_id, status="proposed", visit_type=d.visit_type, due_date=d.due_date, score=d.score,
                          urgency=d.urgency, reason=recall.deterministic_reason(d), batch_date=clock.today().isoformat())
        service.approve([d.patient_id for d in batch], staff="front desk", source="demo")
        scripted = {0: ["Yes please", "1"], 1: ["ok", "2"], 2: ["Yes", "1"], 3: ["No thanks"], 4: ["STOP"],
                    5: ["yes", "3"]}
        for i, d in enumerate(crowd):
            for text in scripted.get(i, []):
                clock.advance(minutes=3)
                service.handle_inbound(patient_id=d.patient_id, text=text, source="demo")
    finally:
        clock._offset = saved_offset
    if channel:
        db.set_setting("channel", channel)
    out = service.run_daily_triage(source="demo")
    return {"status": "ready", "yesterday_batch": len(batch), "today_batch": len(out.get("batch") or [])}


if __name__ == "__main__":
    print(reset_and_play())
