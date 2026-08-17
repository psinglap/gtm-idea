from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query,
                     Request, Security)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import warmgraph.connections as connections
from warmgraph.auth import key_is_valid
from warmgraph.connections import crypto, google
from warmgraph.entities import DoNotContact, norm_email
from warmgraph.models import utcnow
from warmgraph.agents.activities import outreach_send
from warmgraph.outreach import ingest, template
from warmgraph.service import WarmgraphService


def _client_ip(request: Request) -> str:
    """Real client IP behind Cloud Run's proxy (X-Forwarded-For), else direct."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# Per-IP rate limit (caps any single abuser; configurable). Default: 20/min.
_RATE_LIMIT = os.getenv("WG_RATE_LIMIT", "20/minute")
limiter = Limiter(key_func=_client_ip)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Start the schedule with the process.

    The separate Railway cron service has never fired once, and from outside a cron that never
    runs is indistinguishable from one that runs and finds nothing to do — the pipeline looked
    healthy for a day while every send was actually being triggered by hand. Keeping the schedule
    in here means one service to deploy, one set of variables, and the same logs as everything
    else.
    """
    from warmgraph.outreach import scheduler
    task = None
    if scheduler.enabled() and scheduler.in_process_enabled():
        task = asyncio.create_task(scheduler.run_forever(service))
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(
    title="Warmgraph — Competitive Intelligence API",
    version="0.1.0",
    description="Headless competitive-intelligence engine. POST a URL, get a CI report.",
    lifespan=lifespan,
)

# CORS: localhost for dev + any *.vercel.app + the browser extension + WG_CORS_ORIGINS.
# The extension's service worker sends `Origin: chrome-extension://<id>`, so it needs the regex
# too — without it every ingest POST from the worker fails preflight.
_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
_origins += [o.strip() for o in os.getenv("WG_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    # luma.com too: the browser automation runs IN a Luma tab and posts what it enumerated
    # straight from there, so its origin is luma.com rather than the extension's.
    allow_origin_regex=r"(https://.*\.vercel\.app|chrome-extension://.*|https://(www\.)?luma\.com)",
    allow_methods=["*"],
    allow_headers=["*"],
)

service = WarmgraphService()

# Wire the rate limiter (returns HTTP 429 when a client exceeds WG_RATE_LIMIT).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# API-key auth: enforced only when WG_API_KEYS is set (open for local dev). /health stays open.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Optional[str] = Security(_api_key_header)) -> None:
    if not key_is_valid(key, service.settings.api_keys):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


class CIRequest(BaseModel):
    url: str
    depth: str = "quick"  # "quick" | "deep"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_enabled": service.registry.has_llm,
            "llm_provider": service.registry.provider_name,
            "store": service.settings.store_backend}


def _icp_source() -> str:
    """Never raises: a broken ICP file must show up here rather than 500 the settings page."""
    from warmgraph.outreach import icp_rules
    try:
        return icp_rules.load().source
    except icp_rules.IcpConfigError as e:
        return f"ERROR: {e}"


@app.get("/outreach/settings")
def outreach_settings() -> dict:
    """What this deployment is actually configured to do.

    These live in environment variables on the host, so the only way to know whether the loop is
    drafting or sending was to open the Railway dashboard — and a local copy of the file is not
    evidence, because it goes stale the moment someone edits a variable. Reading the values back
    out of the running process is the only answer that cannot be out of date.
    """
    import os
    from warmgraph.agents.activities.outreach_daily import OutreachDailyInput
    from warmgraph.outreach import ingest, scheduler, template
    mode = (os.getenv("WG_OUTREACH_MODE", "draft") or "draft").strip().lower()
    return {
        "mode": mode if mode in ("draft", "send") else "draft",
        "event_horizon_days": ingest.EVENT_HORIZON_DAYS,
        "event_max_age_days": template.MAX_EVENT_AGE_DAYS,
        "scan_lookback_days": ingest.SCAN_LOOKBACK_DAYS,
        "daily_cap": int(os.getenv("WG_OUTREACH_DAILY_CAP", "200") or 200),
        "hourly_cap": int(os.getenv("WG_OUTREACH_HOURLY_CAP", "30") or 30),
        "send_to_catchall": (os.getenv("WG_SEND_TO_CATCHALL", "") or "").strip().lower()
                            in ("1", "true", "yes", "on"),
        # The cron service owns the timetable now, so this reports whether the schedule is ON
        # rather than pretending to know when it fires — the times live in the Railway cron
        # expression, and a second copy in here would be a second thing to keep in step.
        # Which ICP is in force. A config file in the wrong place is indistinguishable from no
        # config file until something says which one is live — and the difference is whether the
        # judge is applying your criteria or the shipped example's.
        "icp_source": _icp_source(),
        "schedule_enabled": scheduler.enabled(),
        "scheduled_by": "cron service" if not scheduler.in_process_enabled() else "the API process",
        # Apollo lookups per run. Reported because it is the pipeline's real throughput limit —
        # the send cap governs how fast people leave the queue, this governs how fast they become
        # sendable at all, and it is the one that has actually been binding.
        "enrich_limit": OutreachDailyInput(url="").enrich_limit,
        "enrich_batch": OutreachDailyInput(url="").enrich_batch,
        "judge_batch": OutreachDailyInput(url="").judge_batch,
    }


@app.post("/competitive-intelligence", dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def competitive_intelligence(request: Request, req: CIRequest) -> dict:
    """Standalone competitive-intelligence report from a URL alone."""
    return service.competitive_intelligence(req.url, req.depth).model_dump(mode="json")


@app.get("/competitive-intelligence/{report_id}", dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def get_ci_report(request: Request, report_id: str) -> dict:
    report = service.get_ci_report(report_id)
    if report is None:
        raise HTTPException(404, "CI report not found")
    return report.model_dump(mode="json")


# --- Generic agent platform: every action + scraper is callable here + via MCP ---
@app.get("/agents", dependencies=[Depends(require_api_key)])
def list_agents() -> dict:
    return {"agents": service.list_agents()}


@app.post("/agents/{name}", dependencies=[Depends(require_api_key)])
@limiter.limit(_RATE_LIMIT)
def run_agent(request: Request, name: str, payload: dict = Body(default_factory=dict)) -> dict:
    try:
        return service.run_agent(name, payload)
    except KeyError:
        raise HTTPException(404, f"Unknown agent: {name}")
    except ValidationError as e:
        raise HTTPException(422, f"Invalid input for agent '{name}': {e}")


# =========================================================================== #
# Event outreach: connections + the browser worker's ingest/queue endpoints.    #
#                                                                              #
# Auth here is the WORKSPACE TOKEN, not the API key. There is no sign-in: the   #
# extension generates a random token, and it maps to one company_id. The token  #
# IS the credential, so these endpoints never accept a company_id directly.     #
# =========================================================================== #
def workspace(token: str = Query(default="", description="Workspace token")) -> str:
    """Resolve a workspace token to its company_id, or 401."""
    cid = connections.company_id_for_token(service.store, token)
    if not cid:
        raise HTTPException(401, "Unknown or missing workspace token")
    return cid


class WorkspaceBody(BaseModel):
    token: str


@app.post("/workspace")
@limiter.limit(_RATE_LIMIT)
def create_workspace(request: Request, url: str = Body(embed=True)) -> dict:
    """Bootstrap: company URL -> (company_id, workspace token). Idempotent — calling it again
    for the same domain returns the SAME token, so a re-run never orphans existing data."""
    profile_domain = url.replace("https://", "").replace("http://", "").split("/")[0].lower()
    client, token = connections.ensure_workspace(service.store, profile_domain)
    return {"company_id": client.id, "domain": client.domain, "token": token}


@app.get("/connections")
def get_connections(cid: str = Depends(workspace)) -> dict:
    return {"connections": connections.connection_status(service.store, cid),
            "readiness": connections.readiness(service.store, cid)}


@app.get("/connect/google")
def connect_google(cid: str = Depends(workspace), token: str = Query(default=""),
                   role: str = Query(default="send"),
                   account: str = Query(default="")) -> dict:
    """Consent URL for one Gmail account.

    role=send    -> stored as `gmail`; this account sends AND is searched.
    role=history -> stored as `gmail_history`; searched only, never sends. Use it for the
                    mailbox where your existing conversations actually live.
    """
    if role not in ("send", "history"):
        raise HTTPException(400, "role must be 'send' or 'history'")
    hint = account or (google.account_hint() if role == "send" else "")
    try:
        # state carries the role so the callback knows which provider to store under.
        return {"auth_url": google.auth_url(state=f"{token}|{role}", login_hint=hint, role=role),
                "account": hint, "role": role}
    except google.GoogleNotConfigured as e:
        raise HTTPException(503, str(e))


@app.get("/oauth/google/callback")
def google_callback(code: str = Query(default=""), state: str = Query(default="")) -> dict:
    """Google redirects here. `state` is `<workspace token>|<role>`."""
    raw_token, _, role = state.partition("|")
    role = role or "send"
    cid = connections.company_id_for_token(service.store, raw_token)
    if not cid:
        raise HTTPException(401, "Unknown workspace token in OAuth state")
    if not code:
        raise HTTPException(400, "Missing authorization code")
    provider = "gmail" if role == "send" else "gmail_history"
    try:
        conn = service.store.upsert_connection(
            google.connect(cid, code, provider=provider, role=role))
    except (google.GoogleAuthError, google.GoogleNotConfigured) as e:
        raise HTTPException(400, str(e))
    except crypto.SecretKeyMissing as e:
        raise HTTPException(503, str(e))
    return {"connected": True, "account": conn.account_label, "role": role,
            "stored_as": conn.provider}


@app.post("/connect/apollo")
def connect_apollo(cid: str = Depends(workspace), api_key: str = Body(embed=True)) -> dict:
    try:
        conn = connections.connect_apollo(service.store, cid, api_key)
    except crypto.SecretKeyMissing as e:
        raise HTTPException(503, str(e))
    if conn.status != "connected":
        raise HTTPException(400, conn.last_error or "Apollo rejected this key")
    return conn.redacted()


class ClaudeLinkBody(BaseModel):
    provider: str
    account_label: str = ""


@app.post("/connect/via-claude")
def connect_via_claude(body: ClaudeLinkBody, cid: str = Depends(workspace)) -> dict:
    """Mark Apollo (or Gmail) as reachable through the user's Claude connector instead of a
    stored credential. Nothing is stored — the connector holds the auth — so this only works
    while a Claude run is driving, which `readiness.unattended_blocked_by` reports."""
    if body.provider not in ("apollo", "gmail"):
        raise HTTPException(400, "only apollo and gmail can be linked via Claude")
    conn = connections.link_via_claude(service.store, cid, body.provider, body.account_label)
    return conn.redacted()


@app.delete("/connections/{provider}")
def disconnect(provider: str, cid: str = Depends(workspace)) -> dict:
    return {"removed": service.store.delete_connection(cid, provider)}


@app.post("/outreach/heartbeat")
def heartbeat(cid: str = Depends(workspace), provider: str = Body(embed=True)) -> dict:
    """Extension says 'I have a live logged-in session for luma/linkedin'. No credential is
    stored — only the fact and the time."""
    if provider not in connections.SESSION_PROVIDERS:
        raise HTTPException(400, f"{provider} is not a session provider")
    return connections.session_ping(service.store, cid, provider).redacted()


class WorkerStatusBody(BaseModel):
    """What the browser worker is doing right now, so the web UI can show it."""
    running: bool = False
    stage: str = ""             # "register" | "scan" | "linkedin" | ""
    reason: str = ""            # what triggered this tick
    last_run_at: str = ""
    last_error: str = ""
    counts: dict = {}           # registered / scanned / guests / profiles read
    next_due_in_min: int = 0    # 0 = due now; the browser wakes often but works rarely
    failures: list = []         # why a registration was skipped — the useful half of "0"


@app.get("/outreach/schedule")
def schedule_status(cid: str = Depends(workspace)) -> dict:
    """When the loop last ran on its own, and what happened.

    The first scheduled run claimed its slot, produced nothing, and left no trace anywhere I could
    read — the only record was a stdout line on the host. This is that record, in the database,
    where the UI can show it.
    """
    from warmgraph.outreach import scheduler
    return {
        "enabled": scheduler.enabled(),
        "runs_at_utc": scheduler.slots(),
        # From memory, not the database. The scheduler runs in this process, so if this endpoint
        # can answer, the task is either alive or the process is gone — which makes an in-memory
        # timestamp exactly as trustworthy as a stored one, and free.
        "alive": scheduler.alive_now(),
        "last": scheduler.last_result(service.store, cid),
        # Per-run, newest first. The cumulative panels answer "where does everything stand" and
        # can never answer "what happened at 2pm".
        "history": scheduler.history(service.store, cid, limit=20),
    }


@app.get("/outreach/funnel")
def outreach_funnel(cid: str = Depends(workspace)) -> dict:
    """The whole thing as a funnel that BALANCES: every stage accounts for the one above it.

    The panels used to be a set of counts side by side, which invited exactly the wrong reading —
    "7,820 waiting" and "10 email found" look like a contradiction until you can see that one is
    upstream of the other. A number that does not subtract from anything cannot show you where
    people are being lost, and where they are lost is the only actionable thing here.

    Each stage carries `out` (people who stop there, with the reason) and `on` (people who
    continue), and on + sum(out) always equals the stage's input.
    """
    from warmgraph.outreach import funnel
    return {"stages": funnel.stages(service.store.event_contact_counts(cid))}


@app.get("/outreach/events-funnel")
def events_funnel(cid: str = Depends(workspace)) -> dict:
    """Events as a funnel, ending where the people funnel begins."""
    from warmgraph.outreach import event_filter, funnel
    events = [e for e in service.store.get_raw_events(since_days=400, limit=3000)
              if e.source == ingest.LUMA]
    regs = {r.event_id: r for r in service.store.get_event_registrations(cid, limit=5000)}
    now = utcnow()
    upcoming_titles = [e.title for e in events if not ingest.has_ended(e, now)]
    keep = {v["title"]: v.get("keep", True) for v in event_filter.classify_cached(
        service.store, cid, service.registry, upcoming_titles)}
    stored = service.store.contacts_per_event(cid)
    upcoming, past = funnel.event_stages(events, regs, now, ingest.EVENT_HORIZON_DAYS,
                                         ingest.SCAN_LOOKBACK_DAYS, keep, stored)
    return {"upcoming": upcoming, "past": past}


@app.get("/outreach/events-status")
def events_status(cid: str = Depends(workspace), days: int = Query(default=7)) -> dict:
    """Events, as a person thinks about them: what am I going to, and what happened each day.

    The Pipeline panel counts CONTACTS, and the browser panel showed the last pass's raw counters
    — which read "registered 0" for hours after a pass that registered nothing, while ten events
    had in fact been registered that day. Neither answered "how many events did I get into".
    """
    from collections import Counter
    from datetime import timedelta

    regs = service.store.get_event_registrations(cid, limit=5000)
    events = {e.id: e for e in service.store.get_raw_events(since_days=400, limit=3000)
              if e.source == ingest.LUMA}
    now = utcnow()

    # Upcoming is the number that matters: 154 invitations sitting in history are noise, the
    # eighteen for events that have not happened yet are a decision.
    upcoming = [r for r in regs
                if events.get(r.event_id) and not ingest.has_ended(events[r.event_id])]

    # "Not registered: 51" and "Invited: 14" said nothing about whether anything was wrong. Both
    # buckets were mostly events we will never register for BY DESIGN — paid, sold out, or not
    # work events — mixed with a couple we genuinely should have. Split by the REASON instead, so
    # the only number that needs attention is the one labelled as needing it.
    from warmgraph.outreach import event_filter
    undecided = [(events[r.event_id], r) for r in upcoming
                 if r.approval_status in ("", "invited")]
    verdicts = {v["title"]: v for v in event_filter.classify_cached(
        service.store, cid, service.registry, [e.title for e, _ in undecided])}

    by_status: dict = {
        "approved": sum(1 for r in upcoming if r.approval_status == "approved"),
        "pending_approval": sum(1 for r in upcoming if r.approval_status == "pending_approval"),
        "waitlist": sum(1 for r in upcoming if r.approval_status == "waitlist"),
        "will_register": 0, "paid": 0, "sold_out": 0, "not_work": 0, "too_far_off": 0,
    }
    for e, r in undecided:
        raw = e.raw or {}
        if not raw.get("is_free", True) or raw.get("price"):
            by_status["paid"] += 1
        elif raw.get("is_sold_out"):
            by_status["sold_out"] += 1
        elif not verdicts.get(e.title, {}).get("keep", True):
            by_status["not_work"] += 1
        elif not ingest.starts_within(e, ingest.EVENT_HORIZON_DAYS):
            by_status["too_far_off"] += 1
        else:
            by_status["will_register"] += 1

    def day_counts(rows, stamp) -> list:
        c = Counter(str(getattr(r, stamp))[:10] for r in rows if getattr(r, stamp))
        out = []
        for i in range(days):
            d = (now - timedelta(days=i)).date().isoformat()
            out.append({"date": d, "count": c.get(d, 0)})
        return list(reversed(out))

    scanned = [r for r in regs if r.scanned_at]
    guests_by_day = Counter()
    for r in scanned:
        guests_by_day[str(r.scanned_at)[:10]] += r.guest_count or 0

    soon = sorted([r for r in upcoming if r.approval_status == "approved"],
                  key=lambda r: events[r.event_id].starts_at or now)[:8]

    return {
        "upcoming": dict(by_status),
        "upcoming_total": len(upcoming),
        "registered_by_day": day_counts([r for r in regs if r.registered_at], "registered_at"),
        "scanned_by_day": [{**d, "guests": guests_by_day.get(d["date"], 0)}
                           for d in day_counts(scanned, "scanned_at")],
        "registered_all_time": len([r for r in regs if r.registered_at]),
        "next_up": [{"title": events[r.event_id].title,
                     "url": events[r.event_id].url,
                     "starts_at": (events[r.event_id].starts_at.isoformat()
                                   if events[r.event_id].starts_at else ""),
                     "guests": r.guest_count or 0}
                    for r in soon],
    }


@app.post("/outreach/worker-status")
def post_worker_status(body: WorkerStatusBody, cid: str = Depends(workspace)) -> dict:
    """The browser half reporting in.

    Steps 1, 2 and 4 run in the user's own Chrome and are otherwise invisible from anywhere
    else — their status lived only in chrome.storage.local, so the deployed UI could not say
    whether anything was happening. Without this the honest answer to "is it running?" was
    "open the extension and look", which is not an answer when the point is to watch it from a
    live URL.
    """
    outreach_send.save_worker_status(service.store, cid, body.model_dump(mode="json"))
    return {"ok": True}


class WorkerLogBody(BaseModel):
    """Activity lines streamed from the browser worker as it works."""
    lines: list = []


@app.post("/outreach/worker-log")
def post_worker_log(body: WorkerLogBody, cid: str = Depends(workspace)) -> dict:
    outreach_send.append_worker_log(service.store, cid, body.lines)
    return {"ok": True}


@app.get("/outreach/worker-log")
def get_worker_log(limit: int = 60, cid: str = Depends(workspace)) -> dict:
    """What the browser is doing, line by line.

    Counts say what a pass achieved but never what it is doing right now, and when the count is
    zero they cannot separate an idle worker from a stuck one.
    """
    return outreach_send.load_worker_log(service.store, cid, limit=min(max(limit, 1), 120))


@app.post("/outreach/run-now")
def post_run_now(background: BackgroundTasks, cid: str = Depends(workspace)) -> dict:
    """One button for the whole loop.

    The two halves are reached differently and there is no way around that: the server half is
    called straight away here, in the background so the click returns immediately; the browser
    half is left a request that the extension collects on its next tick, because Chrome on a desk
    has no address to call. So "started" honestly means started-and-queued, and the activity log
    is where the rest of the story shows up.
    """
    from warmgraph.outreach import scheduler
    row = outreach_send.request_run(service.store, cid, source="ui")
    client = service.store.get_company_by_id(cid)
    url = (client.url or client.domain or "") if client else ""
    if url:
        # run_and_record, not run_agent. Calling the agent directly meant a run started from this
        # button produced no history row and no report email — you pressed it, work happened, and
        # nothing on the page or in the inbox said so. Recording must not depend on who asked.
        background.add_task(scheduler.run_and_record, service,
                            url if url.startswith("http") else f"https://{url}", "manual")
    return {"ok": True, "requested": row, "server_half": bool(url)}


@app.get("/outreach/run-now")
def get_run_now(cid: str = Depends(workspace)) -> dict:
    """Polled by the extension. `seq` increments per request; the worker runs when it sees a
    number higher than the one it last handled."""
    return outreach_send.pending_run(service.store, cid)


@app.get("/outreach/worker-status")
def get_worker_status(cid: str = Depends(workspace)) -> dict:
    return outreach_send.load_worker_status(service.store, cid)


class EventsBody(BaseModel):
    entries: list = []


@app.post("/outreach/events")
def post_events(body: EventsBody, cid: str = Depends(workspace)) -> dict:
    """Everything the extension enumerated on Luma (registered + discovered)."""
    return ingest.ingest_events(service.store, cid, body.entries)


@app.get("/outreach/scan-queue")
def scan_queue(cid: str = Depends(workspace),
               lookback_days: int = Query(default=ingest.SCAN_LOOKBACK_DAYS)) -> dict:
    """Events that have ended, are approved, and show their guest list — no cap, since a scan
    is one API call and the LinkedIn budget downstream is the real governor.

    Also returns what is NOT in the queue and why. An empty list is the normal steady state — the
    work is done until another event finishes — but "0 past events with a readable guest list"
    reads as a failure, and there was no way to tell it apart from one.
    """
    from datetime import timedelta
    now = utcnow()
    floor = now - timedelta(days=max(1, lookback_days))
    regs = service.store.get_event_registrations(cid, limit=5000)
    events = {e.id: e for e in service.store.get_raw_events(since_days=400, limit=3000)
              if e.source == ingest.LUMA}
    done = hidden = 0
    for r in regs:
        e = events.get(r.event_id)
        if not e or not ingest.has_ended(e, now) or r.approval_status != "approved":
            continue
        ended = ingest.parse_datetime(str((e.raw or {}).get("end_at") or "")) or e.starts_at
        if not ended or ended < floor:
            continue
        if not r.scannable:
            hidden += 1
        elif r.scanned_at:
            done += 1

    return {"events": [
        {"event_id": e.id, "url": e.url, "title": e.title,
         "luma_event_id": (e.raw or {}).get("luma_event_id", ""),
         "ticket_key": reg.ticket_key,
         "guest_count": reg.guest_count}
        for e, reg in ingest.pending_scans(service.store, cid, lookback_days)],
        "already_read": done,
        "guest_list_hidden": hidden,
        "window_days": lookback_days}


@app.get("/outreach/register-queue")
def register_queue(cid: str = Depends(workspace), limit: int = Query(default=25),
                   horizon_days: int = Query(default=ingest.EVENT_HORIZON_DAYS)) -> dict:
    """Free, not-sold-out, not-yet-registered events starting within `horizon_days`.

    The `invited` backlog comes first: those are already targeted at the user and are the bulk
    of the missed opportunity."""
    regs = {r.event_id: r for r in service.store.get_event_registrations(cid, limit=2000)}
    rows = [(e, regs.get(e.id)) for e in service.store.get_raw_events(since_days=120, limit=1000)
            if e.source == ingest.LUMA and not ingest.has_ended(e)
            and ingest.starts_within(e, horizon_days)]
    rows = [(e, r) for e, r in rows if ingest.registerable(e, r)]
    # Drop what people attend to relax rather than to work: run clubs, poker nights, book
    # clubs, office hours, student events. Cached per title so a given event is judged once.
    from warmgraph.outreach import event_filter
    verdicts = {v["title"]: v for v in event_filter.classify_cached(
        service.store, cid, service.registry, [e.title for e, _ in rows])}
    rows = [(e, r) for e, r in rows if verdicts.get(e.title, {}).get("keep", True)]
    # The `invited` backlog first: those events are already targeted at this user and yield
    # nothing until converted, which is the whole point of auto-registration.
    rows.sort(key=lambda pair: (0 if (pair[1] and pair[1].approval_status == "invited") else 1,
                                pair[0].starts_at or utcnow()))
    return {"events": [{"event_id": e.id, "url": e.url, "title": e.title,
                        # Luma's own api_id. The browser confirms a registration by looking the
                        # event up in Luma's feed, and matching on a slug parsed out of our url
                        # only works while the two agree.
                        "luma_event_id": (e.raw or {}).get("luma_event_id", ""),
                        "starts_at": e.starts_at.isoformat() if e.starts_at else "",
                        "was_invited": bool(r and r.approval_status == "invited")}
                       for e, r in rows[:limit]]}


class RegisteredBody(BaseModel):
    event_id: str
    approval_status: str = "approved"   # or pending_approval when the host must approve


@app.post("/outreach/registered")
def post_registered(body: RegisteredBody, cid: str = Depends(workspace)) -> dict:
    reg = service.store.get_event_registration(cid, body.event_id)
    if reg is None:
        raise HTTPException(404, "Unknown event for this workspace")
    reg.approval_status = body.approval_status
    reg.registered_at = utcnow().isoformat()
    service.store.upsert_event_registration(reg)
    return {"event_id": reg.event_id, "approval_status": reg.approval_status}


class GuestsBody(BaseModel):
    event_id: str
    guests: list = []
    # Your own LinkedIn URL(s). You appear on your own guest lists, and dropping yourself here
    # avoids paying for a profile read and an Apollo credit before own-domain catches it later.
    self_linkedin: list = []


@app.post("/outreach/guests")
def post_guests(body: GuestsBody, cid: str = Depends(workspace)) -> dict:
    event = service.store.get_raw_event(body.event_id)
    if event is None:
        raise HTTPException(404, "Unknown event")
    from warmgraph.entities import contact_key
    # The user's OWN LinkedIn always counts as self, whether or not the caller remembered to send
    # it. Leaving this to the caller put the operator's own profile in their outreach queue — one LinkedIn
    # read, one Apollo credit and very nearly an email to herself. The answer bank already holds
    # the profile, so there is no reason for it to be optional.
    from warmgraph.agents.activities import outreach_send as _os
    own = [(_os.load_answers(service.store, cid) or {}).get("linkedin", "")]
    self_keys = {contact_key(u) for u in list(body.self_linkedin) + own if u}
    out = ingest.ingest_guests(service.store, cid, event, body.guests, self_keys=self_keys)
    ingest.mark_scanned(service.store, cid, event.id)
    return out


@app.get("/outreach/linkedin-queue")
def linkedin_queue(cid: str = Depends(workspace), browser_id: str = Query(default="browser"),
                   limit: int = Query(default=25)) -> dict:
    """Lease a batch of profiles to read. Leases expire, so a browser that quits mid-run
    releases its rows automatically."""
    service.store.release_expired_leases(cid)
    rows = service.store.lease_event_contacts(cid, leased_by=browser_id, limit=limit)
    return {"contacts": [{"contact_id": c.id, "name": c.name, "linkedin_url": c.linkedin_url}
                         for c in rows]}


class LinkedinResultBody(BaseModel):
    contact_id: str
    headline: str = ""
    profile_text: str = ""
    gated: bool = False


@app.post("/outreach/linkedin-result")
def post_linkedin_result(body: LinkedinResultBody, cid: str = Depends(workspace)) -> dict:
    """One profile, checkpointed the moment it is read — so a laptop closed mid-run loses at
    most the single profile in flight."""
    c = ingest.record_linkedin(service.store, cid, body.contact_id, body.headline,
                               body.profile_text, body.gated)
    if c is None:
        raise HTTPException(404, "Unknown contact for this workspace")
    return {"contact_id": c.id, "status": c.status}


@app.get("/outreach/summary")
def outreach_summary(cid: str = Depends(workspace), limit: int = Query(default=100)) -> dict:
    """Everything the Event Outreach tab renders."""
    store = service.store
    # Only events THIS workspace has a registration for: `raw_events` is shared between clients
    # who attend the same event, so it must never be listed unfiltered.
    regs = {r.event_id: r for r in store.get_event_registrations(cid, limit=2000)}
    events = [e for e in store.get_raw_events(since_days=90, limit=500)
              if e.source == ingest.LUMA and e.id in regs]
    return {
        "queue": store.count_event_contacts(cid),
        "events": [{
            "event_id": e.id, "title": e.title, "url": e.url,
            "short_name": regs[e.id].short_name,
            "short_name_source": regs[e.id].short_name_source,
            "starts_at": e.starts_at.isoformat() if e.starts_at else "",
            "approval_status": regs[e.id].approval_status,
            "guest_count": regs[e.id].guest_count,
            "scanned": bool(regs[e.id].scanned_at),
            "blocked_reason": regs[e.id].blocked_reason,
        } for e in events[:limit]],
        "messages": [m.model_dump(mode="json") for m in
                     store.get_outreach_messages(cid, limit=limit)],
        "connections": connections.connection_status(store, cid),
    }


class ShortNameBody(BaseModel):
    event_id: str
    short_name: str


@app.post("/outreach/event-short-name")
def set_short_name(body: ShortNameBody, cid: str = Depends(workspace)) -> dict:
    """The subject line shortener is rule-based and occasionally graceless. Editing it here
    sticks: `short_name_edited` stops the next Luma re-sync from overwriting it."""
    reg = service.store.get_event_registration(cid, body.event_id)
    if reg is None:
        raise HTTPException(404, "Unknown event for this workspace")
    reg.short_name = body.short_name.strip()
    reg.short_name_edited = True
    service.store.upsert_event_registration(reg)
    return {"event_id": reg.event_id, "short_name": reg.short_name}


class TemplateBody(BaseModel):
    subject: str
    body: str


@app.get("/outreach/template")
def get_template(cid: str = Depends(workspace)) -> dict:
    tmpl = outreach_send.load_template(service.store, cid)
    return {**tmpl.to_dict(), "fields": list(template.FIELDS),
            "unknown_fields": template.unknown_fields(tmpl)}


@app.post("/outreach/template")
def set_template(body: TemplateBody, cid: str = Depends(workspace)) -> dict:
    """You write the email once — calendar link and all, inline. We substitute {first_name},
    {event_name}, {event_short} and {when}, and send it exactly as written."""
    tmpl = template.MessageTemplate(subject=body.subject, body=body.body)
    unknown = template.unknown_fields(tmpl)
    if unknown:
        raise HTTPException(400, f"Unknown field(s): {', '.join('{%s}' % f for f in unknown)}. "
                                 f"Available: {', '.join('{%s}' % f for f in template.FIELDS)}")
    outreach_send.save_template(service.store, cid, tmpl)
    return tmpl.to_dict()


class ClassifyBody(BaseModel):
    titles: list = []


@app.post("/outreach/classify-events")
def classify_events(body: ClassifyBody, cid: str = Depends(workspace)) -> dict:
    """Split a list of event titles into professional vs leisure.

    Fails open: with no LLM everything is kept, because a filter that silently empties the
    pipeline when a model is unavailable is worse than no filter."""
    from warmgraph.outreach import event_filter
    kept, rejected = event_filter.split_cached(
        service.store, cid, service.registry, [str(t) for t in body.titles])
    return {"kept": len(kept), "rejected": rejected}


class QuestionsBody(BaseModel):
    questions: list = []          # [{id,label,type,required,options}] scraped during a run
    event: str = ""               # the event title, so a question for a speaker is about THIS talk


@app.post("/outreach/plan-answers")
def plan_registration_answers(body: QuestionsBody, cid: str = Depends(workspace)) -> dict:
    """One event's scraped form -> {filled, open}. THE resolver: the browser scrapes and clicks,
    and every decision about what a question means is made here.

    The browser deliberately has no copy of this logic. It had a second, simpler one once — a
    normaliser missing two passes — and the two disagreed silently, so every stored answer missed
    and forms were reported unanswerable when the bank held the answer.
    """
    from warmgraph.outreach import answer_llm, registration
    answers = registration.merge_defaults(outreach_send.load_answers(service.store, cid))
    filled, open_qs = registration.plan_answers(body.questions, answers)
    # Then the discretionary gaps — a question for a speaker, a "bingo card" — written from the
    # company facts rather than parked waiting for a human. Private facts are never sent.
    filled, open_qs = answer_llm.fill_open(service.registry, filled, open_qs, answers,
                                           event=body.event)
    return {"filled": filled, "open": open_qs}


def registration_key(q: dict) -> str:
    """The answer bank's key for a question, for rows that arrive without one."""
    from warmgraph.outreach import registration
    return registration.normalise(q.get("label") or "")


@app.post("/outreach/registration-questions")
def post_registration_questions(body: QuestionsBody, cid: str = Depends(workspace)) -> dict:
    """Store what a run could not answer. Overwrites the previous list — an event we could not
    register is skipped and retried tomorrow, so nothing accumulates.

    These have ALREADY been through the resolver: /outreach/plan-answers decided they were open,
    which is why the browser gave up on the event. This used to re-run plan_answers and the LLM
    over them, and the second pass disagreed — the model answered a question the first call had
    declined, so the list saved empty. The event stayed blocked, and the question that blocked it
    never reached anyone. Worst of both outcomes, from asking the same question twice.

    The answer bank is still consulted, because that lookup is deterministic: a question whose
    answer is stored is answered, today and tomorrow, the same way. What is NOT re-run is the
    model — asking it twice about the same question is what produced two different verdicts and an
    empty list.
    """
    from warmgraph.outreach import registration
    answers = registration.merge_defaults(outreach_send.load_answers(service.store, cid))
    _filled, unanswered = registration.plan_answers(body.questions, answers)

    seen, open_qs = set(), []
    for q in unanswered:            # the same question at ten events is one question to answer
        key = q.get("key") or registration_key(q)
        if key in seen:
            continue
        seen.add(key)
        open_qs.append({**q, "key": key})
    outreach_send.save_open_questions(service.store, cid, open_qs)
    return {"open_questions": open_qs}


@app.get("/outreach/questions")
def list_open_questions(cid: str = Depends(workspace)) -> dict:
    return {"open_questions": outreach_send.load_open_questions(service.store, cid),
            "answers": outreach_send.load_answers(service.store, cid)}


class ParseBody(BaseModel):
    reply: str = ""


@app.post("/outreach/parse-answers")
def parse_answers(body: ParseBody, cid: str = Depends(workspace)) -> dict:
    """Map one free-text reply onto the open questions. WRITES NOTHING.

    Parsing and saving are deliberately separate: an LLM attaching the wrong answer to the wrong
    question gets submitted to a real host, and that is invisible unless the mapping is put in
    front of a human first."""
    from warmgraph.outreach import answer_parse
    questions = outreach_send.load_open_questions(service.store, cid)
    mapped = answer_parse.parse_answers(service.registry, body.reply, questions)
    return {
        # `options` travels with the row so a click-only dropdown is shown as a picker rather
        # than a text box the user can type an unusable answer into.
        "mapped": [{"key": q["key"], "label": q["label"], "answer": mapped.get(q["key"], ""),
                    "options": q.get("options") or []}
                   for q in questions],
        "matched": len(mapped),
        "unmatched": [q["label"] for q in answer_parse.unanswered(questions, mapped)],
    }


class AnswersBody(BaseModel):
    answers: dict = {}


@app.post("/outreach/answers")
def post_answers(body: AnswersBody, cid: str = Depends(workspace)) -> dict:
    """Save confirmed answers, keyed by normalised question text so the same question at a
    future event fills itself."""
    from warmgraph.outreach import registration
    questions = outreach_send.load_open_questions(service.store, cid)
    by_key = {q["key"]: q for q in questions}
    bank = {}
    for key, answer in (body.answers or {}).items():
        if not answer:
            continue
        q = by_key.get(key)
        bank[registration.normalise(q["label"]) if q else key] = answer
    outreach_send.save_answers(service.store, cid, bank)

    merged = registration.merge_defaults(outreach_send.load_answers(service.store, cid))
    still = [q for q in questions if not registration.answer_for(q["label"], merged)]
    outreach_send.save_open_questions(service.store, cid, still)
    return {"saved": len(bank), "still_open": len(still)}


class DncBody(BaseModel):
    values: list = []
    reason: str = ""


@app.post("/outreach/do-not-contact")
def add_do_not_contact(body: DncBody, cid: str = Depends(workspace)) -> dict:
    rows = []
    for raw in body.values:
        value = norm_email(str(raw))
        if not value:
            continue
        rows.append(DoNotContact(company_id=cid, value=value,
                                 kind="email" if "@" in value else "domain",
                                 reason=body.reason))
    return {"added": service.store.save_do_not_contact(rows)}


@app.get("/outreach/do-not-contact")
def list_do_not_contact(cid: str = Depends(workspace)) -> dict:
    return {"values": [d.model_dump(mode="json")
                       for d in service.store.get_do_not_contact(cid)]}


# =========================================================================== #
# The UI, served by this same process.                                          #
#                                                                              #
# Mounted LAST, on purpose. Every API route above is already registered, and    #
# FastAPI matches registered routes before a mount — so "/health" and           #
# "/outreach/..." keep working and only unmatched paths fall through to the     #
# bundle. Mounting earlier would shadow the entire API with a static file       #
# handler, which fails as a 404 on routes that plainly exist.                   #
#                                                                              #
# Serving the UI here rather than on a separate host means one URL, one deploy, #
# and no CORS: the bundle is built with an empty API base and calls back        #
# relatively, so it can never point at the wrong backend.                       #
# =========================================================================== #
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

if os.path.isdir(_STATIC):
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=os.path.join(_STATIC, "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        """Any unmatched path returns index.html, so client-side routes survive a refresh.

        An unknown /outreach/... path is a genuine 404 and must stay one — returning HTML for a
        mistyped API call turns a clear error into a confusing one.
        """
        if full_path.startswith(("outreach/", "agents/", "connect/", "oauth/",
                                 "workspace", "health", "connections")):
            raise HTTPException(404, "Not Found")
        return FileResponse(os.path.join(_STATIC, "index.html"))
