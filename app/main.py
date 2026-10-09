"""FastAPI app: staff dashboard (login required), patient-phone simulator, WhatsApp webhook, /healthz."""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import __version__, clock, db, trace
from app.channels.adapters import get_adapter, parse_wa_payload, verify_signature
from app.core import memory, messaging, metrics, recall, scheduling
from app.core.lang import LANG_NAMES, fmt_when
from app.llm import factory
from app.settings import ROOT, clinic_config, settings

log = logging.getLogger("recallcare.web")
WEB = Path(__file__).resolve().parent / "web"
EVALS = ROOT / "evals"

app = FastAPI(title="RecallCare", docs_url=None, redoc_url=None, openapi_url=None)
_session_secret = settings.session_secret or secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax",
                   https_only=settings.public_base_url.startswith("https://"),
                   max_age=12 * 3600, session_cookie="rc_session")
app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
templates = Jinja2Templates(directory=WEB / "templates")

_dashboard_password = settings.dashboard_password or secrets.token_urlsafe(9)
if not settings.dashboard_password:
    log.warning("DASHBOARD_PASSWORD not set; generated one for this process: %s", _dashboard_password)
    print(f"[recallcare] DASHBOARD_PASSWORD not set — temporary password for user '{settings.dashboard_user}': "
          f"{_dashboard_password}", flush=True)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    if not db.q1("SELECT id FROM patients LIMIT 1"):
        from app.demo import reset_and_play
        reset_and_play()
    if settings.auto_jobs:
        threading.Thread(target=_jobs_loop, name="recallcare-jobs", daemon=True).start()


def _jobs_loop() -> None:
    """Tier-1 autonomy on a timer: the morning triage scan and the single re-nudge, within sending hours only."""
    from app.service import run_daily_triage, run_renudges
    last_renudge_hour = None
    while True:
        try:
            now = clock.now()
            lo, hi = clinic_config()["outreach"]["send_hours"]
            if lo <= now.strftime("%H:%M") <= hi:
                ran_today = db.q1("SELECT 1 FROM runs WHERE event_type='daily_triage' AND started_at >= ?",
                                  (now.date().isoformat(),))
                if not ran_today:
                    run_daily_triage(source="scheduler")
                if last_renudge_hour != now.hour:
                    run_renudges(source="scheduler")
                    last_renudge_hour = now.hour
        except Exception:
            log.exception("scheduled job failed")
        time.sleep(300)


# ---------------------------------------------------------------- auth
_login_attempts: dict[str, deque] = defaultdict(deque)


def staff(request: Request) -> str | None:
    return request.session.get("user")


def _need_login(request: Request) -> RedirectResponse | None:
    return None if staff(request) else RedirectResponse("/login", status_code=303)


def _api_auth(request: Request) -> str:
    user = staff(request)
    if not user:
        raise HTTPException(401, "login required")
    return user


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": error, "clinic": clinic_config()["clinic"]})


@app.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "?"
    q = _login_attempts[ip]
    now = time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 5:
        return RedirectResponse("/login?error=Too+many+attempts.+Wait+a+minute.", status_code=303)
    q.append(now)
    form = await request.form()
    user = _check_credentials(str(form.get("username", "")), str(form.get("password", "")))
    if not user:
        return RedirectResponse("/login?error=Wrong+username+or+password", status_code=303)
    request.session["user"] = user
    return RedirectResponse("/", status_code=303)


def _check_credentials(username: str, password: str) -> str | None:
    """Staff account, plus an optional separate judge account (JUDGE_PASSWORD) for the organisers.
    Both compared in constant time; every action in the trace records which account did it."""
    accounts = [(settings.dashboard_user, _dashboard_password)]
    if settings.judge_password:
        accounts.append((settings.judge_user, settings.judge_password))
    match = None
    for u, pw in accounts:
        ok_user = hmac.compare_digest(username.encode(), u.encode())
        ok_pw = hmac.compare_digest(password.encode(), pw.encode())
        if ok_user and ok_pw:
            match = u
    return match


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- shared page context
def _eval_summary() -> dict[str, Any] | None:
    for name in ("report_live.json", "report_mock.json"):
        f = EVALS / name
        if f.exists():
            rep = json.loads(f.read_text(encoding="utf-8"))
            return {"mode": rep["mode"], "model": rep["model"], "golden": rep["by_category"].get("golden"),
                    "adversarial": rep["by_category"].get("adversarial"), "generated_at": rep["generated_at"],
                    "llm_calls": rep["llm_calls"], "tokens": rep["tokens_in"] + rep["tokens_out"]}
    return None


def ctx(request: Request, page: str, **kw: Any) -> dict[str, Any]:
    open_esc = db.q1("SELECT COUNT(*) n FROM escalations WHERE status='open'")["n"]
    tokens = trace.token_totals()
    from app.llm import budget
    usage = budget.totals()
    return {"page": page, "user": staff(request), "clinic": clinic_config()["clinic"],
            "channel": messaging.current_channel(), "llm": factory.provider_name(),
            "wa_ready": bool(settings.wa_access_token and settings.wa_phone_number_id and settings.wa_graph_version),
            "open_escalations": open_esc, "tokens": tokens, "now": clock.now(), "version": __version__,
            "langs": LANG_NAMES, "eval_summary": _eval_summary(), "usage": usage, **kw}


def _esc_rows(status: str = "open") -> list[dict[str, Any]]:
    rows = db.q("""SELECT e.*, p.full_name, p.preferred_language FROM escalations e JOIN patients p ON p.id=e.patient_id
                   WHERE e.status=? ORDER BY CASE e.urgency WHEN 'urgent' THEN 0 WHEN 'soon' THEN 1 ELSE 2 END, e.id DESC""",
                (status,))
    return rows


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def today_page(request: Request):
    if (r := _need_login(request)):
        return r
    batch = messaging_batch()
    pending = db.q("""SELECT m.*, p.full_name, p.preferred_language FROM messages m JOIN patients p ON p.id=m.patient_id
                      WHERE m.status='pending_approval' ORDER BY m.id""")
    manual = recall.no_consent_overdue()[:8]
    recent = db.q("""SELECT f.patient_id, f.status, f.updated_at, p.full_name, p.preferred_language
                     FROM followups f JOIN patients p ON p.id=f.patient_id
                     WHERE f.status IN ('contacted','replied','booked','declined','escalated','opted_out','no_response')
                     ORDER BY f.updated_at DESC LIMIT 12""")
    return templates.TemplateResponse(request, "today.html", ctx(
        request, "today", batch=batch, escalations=_esc_rows(), pending=pending, manual=manual, recent=recent,
        headline=metrics.headline()))


def messaging_batch() -> list[dict[str, Any]]:
    from app.service import proposed_batch
    rows = proposed_batch()
    for r in rows:
        r["age"] = recall.age_on(r["birth_date"], clock.today())
    return rows


@app.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, pid: int | None = None, gloss: int = 1):
    if (r := _need_login(request)):
        return r
    threads = db.q("""SELECT p.id, p.full_name, p.preferred_language, p.sensitive, f.status,
                             (SELECT body FROM messages m WHERE m.patient_id=p.id ORDER BY m.id DESC LIMIT 1) AS last_body,
                             (SELECT ts FROM messages m WHERE m.patient_id=p.id ORDER BY m.id DESC LIMIT 1) AS last_ts
                      FROM patients p JOIN followups f ON f.patient_id=p.id
                      WHERE EXISTS (SELECT 1 FROM messages m WHERE m.patient_id=p.id)
                      ORDER BY last_ts DESC""")
    sel = pid or (threads[0]["id"] if threads else None)
    detail = _patient_detail(sel) if sel else None
    return templates.TemplateResponse(request, "conversations.html", ctx(
        request, "conversations", threads=threads, sel=sel, detail=detail, gloss=gloss))


def _patient_detail(pid: int) -> dict[str, Any]:
    p = messaging.patient(pid)
    msgs = db.q("SELECT * FROM messages WHERE patient_id=? ORDER BY id", (pid,))
    booking = scheduling.active_booking(pid)
    return {"p": p, "f": memory.get(pid) or {}, "msgs": msgs, "age": recall.age_on(p["birth_date"], clock.today()),
            "booking": ({**booking, "when": fmt_when(clock.parse(booking["start_ts"]), "en"),
                         "dentist": scheduling.dentist_name(booking["dentist_id"])} if booking else None),
            "escalations": db.q("SELECT * FROM escalations WHERE patient_id=? ORDER BY id DESC", (pid,)),
            "runs": db.q("SELECT * FROM runs WHERE patient_id=? ORDER BY started_at DESC LIMIT 15", (pid,)),
            "window_open": memory.window_open(pid)}


@app.get("/trace", response_class=HTMLResponse)
def trace_page(request: Request, run: str | None = None, pid: int | None = None, follow: int = 0):
    if (r := _need_login(request)):
        return r
    where, params = ("WHERE r.patient_id=?", (pid,)) if pid else ("", ())
    runs = db.q(f"""SELECT r.*, p.full_name FROM runs r LEFT JOIN patients p ON p.id=r.patient_id {where}
                    ORDER BY r.started_at DESC, r.rowid DESC LIMIT 60""", params)
    sel = run or next((x["run_id"] for x in runs if x["event_type"] == "inbound_message"), runs[0]["run_id"] if runs else None)
    events = trace.run_events(sel) if sel else []
    run_row = db.q1("SELECT r.*, p.full_name FROM runs r LEFT JOIN patients p ON p.id=r.patient_id WHERE run_id=?", (sel,)) if sel else None
    inbound = db.q1("SELECT * FROM messages WHERE run_id=? AND direction='in'", (sel,)) if sel else None
    return templates.TemplateResponse(request, "trace.html", ctx(
        request, "trace", runs=runs, sel=sel, events=events, run=run_row, inbound=inbound, pid=pid, source="app",
        follow=(run is None or follow == 1), newest=runs[0]["run_id"] if runs else ""))


@app.get("/api/trace/latest")
def api_trace_latest(request: Request, pid: int | None = None):
    """Newest run and how many steps it has so far: lets an open Trace page follow new activity live."""
    _api_auth(request)
    where, params = ("WHERE patient_id=?", (pid,)) if pid else ("", ())
    r = db.q1(f"SELECT run_id, outcome FROM runs {where} ORDER BY started_at DESC, rowid DESC LIMIT 1", params)
    if not r:
        return {"run_id": None, "events": 0, "done": True}
    n = db.q1("SELECT COUNT(*) n FROM trace_events WHERE run_id=?", (r["run_id"],))["n"]
    return {"run_id": r["run_id"], "events": n, "done": r["outcome"] is not None}


@app.get("/api/conversations/{pid}/latest")
def api_conversation_latest(request: Request, pid: int):
    """Id of the newest message for one patient: lets an open conversation refresh when a new message arrives."""
    _api_auth(request)
    return {"last_id": db.q1("SELECT COALESCE(MAX(id), 0) n FROM messages WHERE patient_id=?", (pid,))["n"]}


@app.get("/impact", response_class=HTMLResponse)
def impact_page(request: Request):
    if (r := _need_login(request)):
        return r
    return templates.TemplateResponse(request, "impact.html", ctx(
        request, "impact", equity=metrics.equity_table(), esc=metrics.escalation_counts(),
        minutes=metrics.staff_minutes_saved(), spend=metrics.token_spend(), headline=metrics.headline(),
        assumptions=clinic_config()["impact_assumptions"]))


@app.get("/evals", response_class=HTMLResponse)
def evals_page(request: Request, mode: str | None = None):
    if (r := _need_login(request)):
        return r
    reports = {}
    for m in ("live", "mock"):
        f = EVALS / f"report_{m}.json"
        if f.exists():
            reports[m] = json.loads(f.read_text(encoding="utf-8"))
    sel = mode if mode in reports else (next(iter(reports)) if reports else None)
    return templates.TemplateResponse(request, "evals.html", ctx(request, "evals", reports=reports, sel=sel,
                                                                 rep=reports.get(sel) if sel else None))


@app.get("/evals/trace/{mode}/{sid}", response_class=HTMLResponse)
def eval_trace(request: Request, mode: str, sid: str):
    if (r := _need_login(request)):
        return r
    f = EVALS / "runs" / mode / f"{sid}.json"
    if mode not in ("mock", "live") or not f.resolve().is_relative_to((EVALS / "runs").resolve()) or not f.exists():
        raise HTTPException(404)
    dump = json.loads(f.read_text(encoding="utf-8"))
    for e in dump["trace"]:
        for k in ("args", "guard_verdict"):
            if isinstance(e.get(k), str):
                try:
                    e[k] = json.loads(e[k])
                except json.JSONDecodeError:
                    pass
    return templates.TemplateResponse(request, "eval_trace.html", ctx(request, "evals", dump=dump, mode=mode))


@app.get("/evals/report.md", response_class=PlainTextResponse)
def eval_report_md(request: Request, mode: str = "mock"):
    _api_auth(request)
    f = EVALS / (f"report_{mode}.md" if mode in ("mock", "live") else "report.md")
    return f.read_text(encoding="utf-8") if f.exists() else "No report yet."


@app.get("/simulator", response_class=HTMLResponse)
def simulator_page(request: Request, pid: int | None = None, embed: int = 0):
    if (r := _need_login(request)):
        return r
    patients = db.q("""SELECT p.id, p.full_name, p.preferred_language, f.status FROM patients p
                       LEFT JOIN followups f ON f.patient_id=p.id
                       ORDER BY CASE WHEN f.status IS NULL THEN 1 ELSE 0 END, p.id""")
    return templates.TemplateResponse(request, "simulator.html", ctx(request, "simulator", patients=patients,
                                                                     pid=pid or 1, embed=embed))


# ---------------------------------------------------------------- JSON actions (staff)
def _run(fn, *a, **kw):
    try:
        return JSONResponse({"ok": True, "result": fn(*a, **kw)})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("action failed")
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.post("/api/triage/run")
def api_triage(request: Request):
    _api_auth(request)
    from app.service import run_daily_triage
    return _run(lambda: {"proposed": len(run_daily_triage().get("batch") or [])})


@app.post("/api/batch/approve")
async def api_approve(request: Request):
    user = _api_auth(request)
    body = await request.json()
    from app.service import approve, proposed_batch
    ids = body.get("patient_ids") or [r["patient_id"] for r in proposed_batch()]
    return _run(approve, [int(i) for i in ids], staff=user)


@app.post("/api/batch/{pid}/remove")
def api_remove(request: Request, pid: int):
    user = _api_auth(request)
    from app.service import remove_from_batch
    return _run(remove_from_batch, pid, staff=user)


@app.post("/api/batch/{pid}/edit")
async def api_edit(request: Request, pid: int):
    user = _api_auth(request)
    body = await request.json()
    from app.service import edit_reason
    return _run(edit_reason, pid, str(body.get("reason", ""))[:200], staff=user)


@app.post("/api/pending/{mid}/{decision}")
def api_pending(request: Request, mid: int, decision: str):
    user = _api_auth(request)
    if decision not in ("approve", "reject"):
        raise HTTPException(400)
    return _run(messaging.release_pending, mid, approve=decision == "approve", staff=user)


@app.post("/api/escalations/{eid}/resolve")
async def api_resolve(request: Request, eid: int):
    user = _api_auth(request)
    body = await request.json()
    from app.service import resolve_escalation
    return _run(resolve_escalation, eid, str(body.get("resolution") or "handled by staff"), staff=user)


@app.post("/api/conversations/{pid}/reply")
async def api_staff_reply(request: Request, pid: int):
    user = _api_auth(request)
    body = await request.json()
    text = str(body.get("text", "")).strip()[:1000]
    if not text:
        raise HTTPException(400, "empty")
    from app.service import staff_reply
    return _run(staff_reply, pid, text, staff=user)


@app.post("/api/channel")
async def api_channel(request: Request):
    _api_auth(request)
    body = await request.json()
    name = body.get("channel")
    if name == "whatsapp":
        try:
            get_adapter("whatsapp")
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return _run(messaging.set_channel, name)


@app.post("/api/demo/reset")
def api_demo_reset(request: Request):
    _api_auth(request)
    from app.demo import reset_and_play
    return _run(reset_and_play)


@app.post("/api/jobs/renudge")
def api_renudge(request: Request):
    _api_auth(request)
    from app.service import run_renudges
    return _run(run_renudges)


# ---------------------------------------------------------------- simulator (acts as the patient's phone)
@app.get("/api/simulator/{pid}/messages")
def api_sim_messages(request: Request, pid: int, after: int = 0):
    _api_auth(request)
    p = messaging.patient(pid)
    rows = db.q("SELECT id, direction, kind, template_name, body, gloss_en, status, ts FROM messages "
                "WHERE patient_id=? AND id>? AND status IN ('sent','received','pending_approval') ORDER BY id", (pid, after))
    return {"patient": {"id": p["id"], "name": p["full_name"], "preferred_name": p["preferred_name"],
                        "language": p["preferred_language"], "opted_out": bool(p["opted_out"])},
            "window_open": memory.window_open(pid), "channel": messaging.current_channel(), "messages": rows}


@app.post("/api/simulator/{pid}/send")
async def api_sim_send(request: Request, pid: int):
    _api_auth(request)
    body = await request.json()
    text = str(body.get("text", ""))[:5000]
    from app.service import handle_inbound
    return _run(handle_inbound, patient_id=pid, text=text, channel="simulator")


# ---------------------------------------------------------------- WhatsApp webhook (public, signed)
@app.get("/webhook/whatsapp")
def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and settings.wa_verify_token and \
            hmac.compare_digest(q.get("hub.verify_token", ""), settings.wa_verify_token):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


@app.post("/webhook/whatsapp")
async def wa_inbound(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        trace_id = trace.new_run("webhook_rejected", None)
        trace.event(trace_id, "supervisor", "reject_unsigned_webhook", outcome="blocked",
                    args={"bytes": len(raw), "has_signature": bool(request.headers.get("X-Hub-Signature-256"))})
        trace.end_run(trace_id, "rejected")
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "bad json")
    inbound = parse_wa_payload(payload)          # parses even if sending isn't configured yet
    for m in inbound:
        background.add_task(_process_wa, m.from_phone, m.text, m.wa_message_id)
    return {"received": len(inbound)}


def _process_wa(phone: str, text: str, wa_id: str | None) -> None:
    from app.service import handle_inbound
    try:
        res = handle_inbound(from_phone=phone, text=text, channel="whatsapp", wa_message_id=wa_id)
        log.info(json.dumps({"type": "wa_inbound", "status": res.get("status"), "run_id": res.get("run_id"),
                             "from": "+••••" + phone[-4:]}))
    except Exception:
        log.exception("processing WhatsApp message failed")


# ---------------------------------------------------------------- health
@app.get("/healthz")
def healthz():
    try:
        n = db.q1("SELECT COUNT(*) n FROM patients")["n"]
        db_ok = True
    except Exception:
        n, db_ok = 0, False
    return JSONResponse({"status": "ok" if db_ok else "degraded", "version": __version__, "db": db_ok,
                         "synthetic_patients": n, "llm_provider": factory.provider_name(),
                         "channel": messaging.current_channel() if db_ok else None,
                         "time_sgt": clock.iso(clock.now())}, status_code=200 if db_ok else 503)
