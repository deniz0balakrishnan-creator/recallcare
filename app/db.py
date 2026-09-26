"""SQLite storage: app data + long-term per-patient memory + decision trace.

Short-term conversation memory lives separately in LangGraph's SQLite checkpointer.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.settings import settings

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS patients (
    id                  INTEGER PRIMARY KEY,
    full_name           TEXT NOT NULL,
    preferred_name      TEXT NOT NULL,
    phone               TEXT NOT NULL UNIQUE,
    preferred_language  TEXT NOT NULL CHECK (preferred_language IN ('en','zh','ms','ta')),
    birth_date          TEXT NOT NULL,
    chas_tier           TEXT NOT NULL DEFAULT 'none',      -- none|blue|orange|green
    generation_card     TEXT NOT NULL DEFAULT 'none',      -- none|pioneer|merdeka
    whatsapp_consent    INTEGER NOT NULL DEFAULT 1,
    sensitive           INTEGER NOT NULL DEFAULT 0,        -- Tier 2: every outbound message needs staff approval
    sensitive_note      TEXT,
    opted_out           INTEGER NOT NULL DEFAULT 0,
    opted_out_at        TEXT,
    preferred_dentist   TEXT,
    synthetic           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS visits (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    visit_date  TEXT NOT NULL,
    visit_type  TEXT NOT NULL,
    dentist_id  TEXT
);

-- Recall schedule: what each patient is due for (interval-based or set by a treatment plan).
CREATE TABLE IF NOT EXISTS recalls (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    visit_type  TEXT NOT NULL,
    due_date    TEXT NOT NULL,
    source      TEXT NOT NULL,          -- interval|treatment_plan
    active      INTEGER NOT NULL DEFAULT 1
);

-- Long-term memory: one follow-up state machine per patient.
-- due -> proposed -> approved -> contacted -> replied -> booked | declined | escalated | opted_out | no_response
CREATE TABLE IF NOT EXISTS followups (
    patient_id          INTEGER PRIMARY KEY REFERENCES patients(id),
    status              TEXT NOT NULL,
    visit_type          TEXT,
    due_date            TEXT,
    score               REAL,
    urgency             TEXT,
    reason              TEXT,
    batch_date          TEXT,
    last_contact_at     TEXT,
    last_inbound_at     TEXT,
    window_expires_at   TEXT,
    nudge_count         INTEGER NOT NULL DEFAULT 0,
    summary_en          TEXT NOT NULL DEFAULT '',
    pending_options     TEXT,           -- JSON list of slot options last offered
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    id           TEXT PRIMARY KEY,      -- e.g. 2026-09-29T09:00|D1
    dentist_id   TEXT NOT NULL,
    start_ts     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'free',   -- free|booked|blocked
    appointment_id INTEGER
);
CREATE INDEX IF NOT EXISTS ix_slots_start ON slots(start_ts);

CREATE TABLE IF NOT EXISTS appointments (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    dentist_id  TEXT NOT NULL,
    start_ts    TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    visit_type  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'booked',   -- booked|cancelled
    created_by  TEXT NOT NULL,                    -- agent:scheduling | staff | seed
    created_at  TEXT NOT NULL,
    run_id      TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    direction   TEXT NOT NULL,          -- in|out
    channel     TEXT NOT NULL,          -- simulator|whatsapp
    kind        TEXT NOT NULL,          -- text|template|staff
    template_name TEXT,
    lang        TEXT,
    body        TEXT NOT NULL,
    gloss_en    TEXT,
    status      TEXT NOT NULL,          -- received|sent|blocked|pending_approval|rejected|failed
    wa_message_id TEXT,
    ts          TEXT NOT NULL,
    run_id      TEXT,
    agent       TEXT
);
CREATE INDEX IF NOT EXISTS ix_messages_patient ON messages(patient_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_wa ON messages(wa_message_id) WHERE wa_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS escalations (
    id          INTEGER PRIMARY KEY,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    category    TEXT NOT NULL,
    urgency     TEXT NOT NULL,          -- routine|soon|urgent
    summary_en  TEXT NOT NULL,
    source_message_id INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',   -- open|resolved
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolution  TEXT,
    run_id      TEXT
);

CREATE TABLE IF NOT EXISTS opt_outs (
    id               INTEGER PRIMARY KEY,
    patient_id       INTEGER NOT NULL REFERENCES patients(id),
    ts               TEXT NOT NULL,
    source_text_hash TEXT NOT NULL,
    run_id           TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    patient_id  INTEGER,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    outcome     TEXT,
    step_count  INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'app'     -- app|eval|demo
);

CREATE TABLE IF NOT EXISTS trace_events (
    id              INTEGER PRIMARY KEY,
    run_id          TEXT NOT NULL,
    ts              TEXT NOT NULL,
    patient_id      INTEGER,
    agent           TEXT NOT NULL,
    action          TEXT NOT NULL,
    args            TEXT,               -- JSON, redacted
    guard_verdict   TEXT,
    model           TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    tokens_estimated INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    outcome         TEXT NOT NULL,      -- ok|blocked|escalated|error|skipped
    thought_summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_trace_run ON trace_events(run_id, id);
CREATE INDEX IF NOT EXISTS ix_trace_patient ON trace_events(patient_id, id);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_lock = threading.RLock()


def db_path() -> Path:
    p = settings.path(settings.database_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(db_path(), timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
    finally:
        c.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction (a small clinic's volume makes a global write lock fine)."""
    with _lock, conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def reset_db() -> None:
    p = db_path()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    init_db()


def q(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def q1(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def get_setting(key: str, default: str | None = None) -> str | None:
    row = q1("SELECT value FROM app_settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
