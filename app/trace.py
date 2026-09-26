"""Decision trace: every agent step, tool call and LLM call becomes a trace_events row + a JSON log line."""
from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from typing import Any

from app import clock, db
from app.llm.base import LLMResult

log = logging.getLogger("recallcare.trace")
if not log.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False

_PHONE = re.compile(r"\+?\d[\d\s-]{6,}(\d{4})")


def redact(value: Any, max_len: int = 160) -> Any:
    """Mask phone numbers (keep last 4 digits) and truncate long strings for the trace."""
    if isinstance(value, str):
        v = _PHONE.sub(lambda m: "+••••" + m.group(1), value)
        return v if len(v) <= max_len else v[: max_len - 1] + "…"
    if isinstance(value, dict):
        return {k: redact(v, max_len) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, max_len) for v in list(value)[:20]]
    return value


def new_run(event_type: str, patient_id: int | None = None, source: str = "app") -> str:
    run_id = f"r-{clock.now().strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    db.execute("INSERT INTO runs(run_id,event_type,patient_id,started_at,source) VALUES(?,?,?,?,?)",
               (run_id, event_type, patient_id, clock.iso(clock.now()), source))
    return run_id


def event(run_id: str, agent: str, action: str, *, outcome: str = "ok", patient_id: int | None = None,
          args: Any = None, guard_verdict: Any = None, llm: LLMResult | None = None,
          thought_summary: str | None = None) -> int:
    row = {
        "run_id": run_id, "ts": clock.iso(clock.now()), "patient_id": patient_id, "agent": agent, "action": action,
        "args": db.dumps(redact(args)) if args is not None else None,
        "guard_verdict": db.dumps(guard_verdict) if guard_verdict is not None else None,
        "model": f"{llm.provider}:{llm.model}{' (fallback)' if llm.fallback_used else ''}" if llm else None,
        "tokens_in": llm.tokens_in if llm else None, "tokens_out": llm.tokens_out if llm else None,
        "tokens_estimated": int(llm.tokens_estimated) if llm else 0,
        "latency_ms": llm.latency_ms if llm else None, "outcome": outcome,
        "thought_summary": (thought_summary or "")[:300] or None,
    }
    with db.tx() as c:
        cur = c.execute(f"INSERT INTO trace_events({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
        if llm:
            c.execute("UPDATE runs SET tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE run_id=?",
                      (llm.tokens_in, llm.tokens_out, run_id))
        eid = cur.lastrowid
    log.info(json.dumps({"level": "info", "type": "trace", **{k: v for k, v in row.items() if k != "args"},
                         "args": redact(args)}, ensure_ascii=False, default=str))
    return eid


def end_run(run_id: str, outcome: str, step_count: int = 0) -> None:
    db.execute("UPDATE runs SET ended_at=?, outcome=?, step_count=? WHERE run_id=?",
               (clock.iso(clock.now()), outcome, step_count, run_id))


def run_events(run_id: str) -> list[dict[str, Any]]:
    rows = db.q("SELECT * FROM trace_events WHERE run_id=? ORDER BY id", (run_id,))
    for r in rows:
        for k in ("args", "guard_verdict"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except json.JSONDecodeError:
                    pass
    return rows


def token_totals(source: str | None = None) -> dict[str, int]:
    where, params = ("WHERE source=?", (source,)) if source else ("", ())
    row = db.q1(f"SELECT COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout, COUNT(*) runs FROM runs {where}", params)
    calls = db.q1("SELECT COUNT(*) n FROM trace_events WHERE model IS NOT NULL")
    return {"tokens_in": row["tin"], "tokens_out": row["tout"], "runs": row["runs"], "llm_calls": calls["n"]}
