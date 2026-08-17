"""Run the loop on a schedule from inside the API process.

There is a separate Railway cron service for this, and it has never fired once. Every send so far
happened because someone triggered it by hand, and from outside that is indistinguishable from a
working schedule with an empty queue — the pipeline looked healthy the entire time.

A cron service is a second thing to deploy, a second set of variables to keep in step, and a
dashboard I cannot read. The API is already running continuously and already holds the database
connection, the credentials and the settings, so the schedule belongs here: one service, one set
of variables, and the same activity log everything else reports into.

Design notes:

  • **Time-based, not interval-based.** The loop wakes every minute and asks "is it past a slot I
    have not run today". A restart therefore changes nothing — an interval timer would drift by
    the uptime of the process, so a redeploy at 14:59 would silently move the 15:00 run.

  • **The marker is per slot per day, in the database.** Two instances, or one instance restarting
    mid-run, cannot double-send: whoever writes the marker first owns that slot.

  • **A missed slot is skipped, not caught up.** If the service was down from 15:00 to 17:00, the
    15:00 slot is gone. Firing it late means two batches close together, which is exactly the
    burst the hourly cap exists to prevent.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import List, Optional

SCHEDULE_KEY = "outreach_schedule_last"
RESULT_KEY = "outreach_schedule_result"
RUN_LEASE_KEY = "outreach_run_lease"

# Minimum gap between two automatic passes, from ANY trigger.
#
# There are two schedulers on purpose — the one in this process and the Railway cron service —
# because having a second way to fire the loop is worth keeping, and the one you can control from
# a dashboard is worth keeping most. What is not worth having is both running the same batch
# minutes apart, which the slot marker alone does not prevent: it only knows about slots, and the
# cron fires on its own clock.
#
# So every automatic trigger takes this lease first. Whichever fires first for a given window
# does the work and the other finds the lease held and returns quietly. 45 minutes is comfortably
# longer than a full pass and comfortably shorter than the three-hour gap between slots.
MIN_RUN_GAP_MINUTES = int(os.getenv("WG_MIN_RUN_GAP_MINUTES", "45") or 45)

# 15:00, 18:00, 21:00 and 23:00 UTC — 8am, 11am, 2pm and 4pm Pacific during daylight time.
# Four slots against a 50/hour cap is 200 a day, reached in four small batches rather than one
# burst. UTC does not follow daylight saving, so these drift an hour in November.
DEFAULT_SLOTS = "15:00,18:00,21:00,23:00"


def slots() -> List[str]:
    raw = os.getenv("WG_RUN_AT_UTC", "") or DEFAULT_SLOTS
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            out.append(f"{int(hh):02d}:{int(mm):02d}")
        except ValueError:
            continue                     # a malformed slot is dropped, never a crash on boot
    return sorted(set(out))


def enabled() -> bool:
    """The one switch, read by the cron service AND by the in-process loop.

    Set it once as a Railway project-level shared variable so both services see the same value —
    it was previously honoured only in here, so turning the schedule "off" left the cron firing.
    """
    return (os.getenv("WG_SCHEDULER", "1") or "1").strip().lower() not in ("0", "false", "no")


def in_process_enabled() -> bool:
    """Whether THIS process should also schedule.

    Off by default. The cron service is the scheduler: a web process and a batch worker are
    different jobs, and running the batch inside the API meant an API crash took the schedule with
    it — which is exactly what happened when the database was cut off and /health stopped
    answering. It also meant a redeploy could kill a run mid-pass, and a 30-minute pass competed
    with request serving for the same container's memory.

    It lives on behind a flag rather than being deleted, because it is proven and the cron service
    was not, and one of them being reachable matters more than which.
    """
    return (os.getenv("WG_IN_PROCESS_SCHEDULER", "0") or "0").strip().lower() in ("1", "true", "yes")


def due_slot(now: datetime, done: str, todays: List[str]) -> str:
    """The slot that should run now, or "".

    `done` is the last marker written, "YYYY-MM-DD HH:MM". Returns the LATEST slot that has
    already passed today and is newer than the marker, so a service that was down over two slots
    runs once rather than twice.
    """
    day = now.date().isoformat()
    passed = [s for s in todays if f"{now:%H:%M}" >= s]
    if not passed:
        return ""
    latest = passed[-1]
    return "" if done >= f"{day} {latest}" else f"{day} {latest}"


HISTORY_KEY = "outreach_run_history"
HISTORY_KEEP = 40


def _browser_activity_since(store, cid: str, since: str) -> dict:
    """Registrations and guest lists the BROWSER completed since the last run row.

    A run report that covers only Apollo, judging and sending describes half the loop. Steps 1 to
    4 — syncing Luma, registering, reading guest lists — happen in Chrome, and a day where none of
    them ran looks identical in the numbers to a day where they all did, right up until the queue
    runs dry a week later.
    """
    from warmgraph.outreach import ingest
    regs = store.get_event_registrations(cid, limit=5000)
    events = {e.id: e for e in store.get_raw_events(since_days=400, limit=3000)
              if e.source == ingest.LUMA}
    newer = lambda stamp: bool(stamp) and str(stamp) > since
    scanned = [r for r in regs if newer(r.scanned_at)]
    upcoming = [r for r in regs
                if events.get(r.event_id) and not ingest.has_ended(events[r.event_id])]
    return {
        "registered": sum(1 for r in regs if newer(r.registered_at)),
        "guest_lists": len(scanned),
        "guests_added": sum(r.guest_count or 0 for r in scanned),
        "events_approved": sum(1 for r in upcoming if r.approval_status == "approved"),
        "events_pending": sum(1 for r in upcoming if r.approval_status == "pending_approval"),
    }


def _append_history(store, cid: str, row: dict) -> None:
    """Keep the last runs, so today's activity is readable without waiting for tomorrow.

    The Events and Pipeline panels are cumulative totals — they answer "where does everything
    stand", never "what happened at 2pm". A total that moves by 28 tells you nothing about which
    run moved it, or whether the other three did anything at all.
    """
    client = store.get_company_by_id(cid)
    if client is None:
        return
    from warmgraph.models import utcnow
    kept = list((client.data or {}).get(HISTORY_KEY) or [])
    kept.append({**row, "at": utcnow().isoformat()})
    client.data = {**(client.data or {}), HISTORY_KEY: kept[-HISTORY_KEEP:]}
    store.upsert_company(client)


def history(store, cid: str, limit: int = 20) -> list:
    client = store.get_company_by_id(cid)
    if client is None:
        return []
    return list((client.data or {}).get(HISTORY_KEY) or [])[-limit:][::-1]


def _record(store, cid: str, result: dict) -> None:
    """Persist what the last scheduled run did, so it can be read from the UI.

    Stdout is not observability when the host's logs are not reachable. Every failure today was
    diagnosed by inference because the one place that knew what happened was a log nobody could
    open.
    """
    from warmgraph.models import utcnow
    client = store.get_company_by_id(cid)
    if client is None:
        return
    client.data = {**(client.data or {}),
                   RESULT_KEY: {**result, "at": utcnow().isoformat()}}
    store.upsert_company(client)


def last_result(store, cid: str) -> dict:
    client = store.get_company_by_id(cid)
    if client is None:
        return {}
    return (client.data or {}).get(RESULT_KEY) or {}


def try_claim_run(store, cid: str, source: str,
                  gap_minutes: int = MIN_RUN_GAP_MINUTES) -> bool:
    """True if this trigger may run now. Shared by every automatic caller.

    Deliberately NOT used by the "Run now" button: a person pressing a button has decided, and
    telling them the loop is busy when they can see it is idle would be worse than a second pass.
    """
    from warmgraph.models import utcnow
    client = store.get_company_by_id(cid)
    if client is None:
        return False
    held = ((client.data or {}).get(RUN_LEASE_KEY) or {}).get("at") or ""
    if held:
        try:
            when = datetime.fromisoformat(held)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if (utcnow() - when).total_seconds() < gap_minutes * 60:
                return False
        except ValueError:
            pass                          # an unparseable lease is treated as no lease
    client.data = {**(client.data or {}),
                   RUN_LEASE_KEY: {"at": utcnow().isoformat(), "source": source}}
    store.upsert_company(client)
    return True


def run_lease(store, cid: str) -> dict:
    client = store.get_company_by_id(cid)
    if client is None:
        return {}
    return (client.data or {}).get(RUN_LEASE_KEY) or {}


def _marker(store, cid: str) -> str:
    client = store.get_company_by_id(cid)
    if client is None:
        return "9999-99-99 99:99"        # unknown workspace: never run
    return (client.data or {}).get(SCHEDULE_KEY) or ""


def _claim(store, cid: str, marker: str) -> bool:
    """Write the marker, and say whether we got there first."""
    client = store.get_company_by_id(cid)
    if client is None:
        return False
    if ((client.data or {}).get(SCHEDULE_KEY) or "") >= marker:
        return False                     # someone else claimed this slot
    client.data = {**(client.data or {}), SCHEDULE_KEY: marker}
    store.upsert_company(client)
    return True


# Liveness, in memory. The scheduler used to prove it was awake by writing a heartbeat row every
# five minutes — which is a database write, forever, to say nothing has changed. It also kept the
# database permanently awake, which is most of what a serverless Postgres bills for.
#
# It runs inside the API process, so if the API can answer at all the task is either alive or the
# process is gone. That makes an in-memory timestamp exactly as trustworthy as a stored one, and
# free.
_alive_at: Optional[datetime] = None
_next_wake: Optional[datetime] = None


def alive_now() -> dict:
    """What the scheduler is doing, read from memory rather than the database."""
    return {"at": _alive_at.isoformat() if _alive_at else "",
            "next_slot": _next_wake.strftime("%Y-%m-%d %H:%M") if _next_wake else ""}


def seconds_until(now: datetime, todays: List[str]) -> float:
    """How long to sleep before the next slot. Never negative, never longer than a day."""
    from datetime import timedelta
    if not todays:
        return 3600.0
    for hhmm in todays:
        hh, mm = (int(x) for x in hhmm.split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if at > now:
            return max(1.0, (at - now).total_seconds())
    hh, mm = (int(x) for x in todays[0].split(":"))
    tomorrow = (now + timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return max(1.0, (tomorrow - now).total_seconds())


async def run_forever(service, log=print) -> None:
    """Sleep until the next slot, run it, sleep again. Never raises.

    It used to wake every 60 seconds and read the database to ask what time it was: all company
    rows, then a marker row per workspace, 1,440 times a day, almost always to conclude there was
    nothing to do. That is why the database never idled.

    The slot times are known, so the wait is computable. Four wake-ups a day, and the database is
    touched only when a run is actually about to happen.

    Still time-based rather than interval-based: it computes the next slot from the clock each
    time, so a restart at 14:59 does not shift the 15:00 run.
    """
    global _alive_at, _next_wake
    from warmgraph.storage import mirror

    while True:
        now = datetime.now(timezone.utc)
        _alive_at = now
        try:
            if not (enabled() and in_process_enabled()):
                # Manual mode, or the cron service is the scheduler. Nothing is read, nothing is
                # written; the UI button and the cron's own --force still work.
                _next_wake = None
                await asyncio.sleep(600)
                continue

            todays = slots()
            wait = seconds_until(now, todays)
            from datetime import timedelta
            _next_wake = now + timedelta(seconds=wait)
            log(f"[scheduler] next run in {wait / 60:.0f} min ({_next_wake:%H:%M} UTC)")
            await asyncio.sleep(wait)

            now = datetime.now(timezone.utc)
            _alive_at = now
            if not (enabled() and in_process_enabled()):   # switched off while we slept
                continue

            todays = slots()
            for url in _workspace_urls(service.store):
                domain = url.split("//")[-1].split("/")[0]
                cid = mirror.client_id_for(service.store, domain)
                marker = due_slot(now, _marker(service.store, cid), todays)
                if not marker or not _claim(service.store, cid, marker):
                    continue
                if not try_claim_run(service.store, cid, "scheduler"):
                    log(f"[scheduler] {marker} skipped — another trigger just ran")
                    continue
                log(f"[scheduler] {marker} running the loop for {url}")
                _record(service.store, cid, {"slot": marker, "state": "running"})
                # to_thread: run_agent is blocking, and blocking the event loop would stall every
                # HTTP request for the length of a full pass.
                report = await asyncio.to_thread(run_and_record, service, url, marker)
                log(f"[scheduler] {marker} done: {report.get('delivered')}")

        except Exception as e:                   # a scheduler that dies stops everything
            log(f"[scheduler] error: {e}")
            await asyncio.sleep(60)


def run_and_record(service, url: str, source: str = "manual",
                   mode: Optional[str] = None) -> dict:
    """Run the loop for one workspace and record what happened. Blocking; safe to call anywhere.

    The scheduler used to do the recording inline, so a run triggered from the UI produced no
    history row and no report email — you pressed the button, work happened, and the page showed
    nothing new. All three callers now go through here, so "what happened" does not depend on who
    asked: the cron service, the in-process scheduler, and the Run Now button.

    This function was deleted by accident while the loop above was rewritten to sleep until the
    next slot rather than wake every 60 seconds. Nothing failed at import — Python resolves a
    global only when it is used — so the API booted, /health stayed green, and the tests kept
    passing because they asserted the STRING "run_and_record" appeared at the call sites rather
    than calling it. The only place it surfaced was a slot actually firing, where the scheduler's
    own `except Exception` swallowed it and went back to sleep. Four scheduled runs a day did
    nothing for a day and a half, silently. See the test that now calls this for real.
    """
    from warmgraph.storage import mirror

    domain = url.split("//")[-1].split("/")[0]
    cid = mirror.client_id_for(service.store, domain)
    # `mode` only when the caller set one, so the agent's own default (WG_OUTREACH_MODE) still
    # applies for the callers that do not pass it.
    payload = {"url": url}
    if mode:
        payload["mode"] = mode
    try:
        report = service.run_agent("outreach_daily", payload)
    except Exception as e:
        # A failed run is recorded exactly like a successful one. A run that crashed and a run
        # that never fired look identical from the outside, and that is the whole reason a day
        # and a half went by without anyone knowing.
        err = f"{type(e).__name__}: {e}"[:400]
        _record(service.store, cid, {"slot": source, "state": "failed", "error": err})
        _append_history(service.store, cid, {"slot": source, "state": "failed", "error": err[:200]})
        report = {"delivered": {"delivered": 0, "skipped": 0, "mode": "?"}, "error": err}
        _after_run(service, url, report)
        return report

    d = report.get("delivered") or {}
    e = report.get("enriched") or {}
    j = report.get("judged") or {}
    _record(service.store, cid, {
        "slot": source, "state": "ok", "delivered": d.get("delivered", 0),
        "report": {k: v for k, v in report.items() if k != "delivered"}})
    _append_history(service.store, cid, {
        "slot": source, "state": "ok",
        "sent": d.get("delivered", 0), "skipped": d.get("skipped", 0),
        "apollo": e.get("attempted", 0), "reused": e.get("reused", 0),
        "emails_found": e.get("enriched", 0),
        "judged": j.get("judged", 0), "targets": j.get("targets", 0),
        "bounced": report.get("bounced", 0),
        "skip_reasons": d.get("skip_reasons") or {}})
    _after_run(service, url, report)
    return report


def _after_run(service, url: str, report: dict) -> None:
    """Ask the browser half to run too, then email the report."""
    from warmgraph.agents.activities import outreach_send
    from warmgraph.outreach import run_report
    from warmgraph.storage import mirror

    domain = url.split("//")[-1].split("/")[0]
    cid = mirror.client_id_for(service.store, domain)
    try:
        outreach_send.request_run(service.store, cid, source="schedule")
    except Exception:
        pass
    run_report.email_run(service.store, cid, report)


def _workspace_urls(store) -> List[str]:
    out = []
    for row in store.raw_scan("companies"):
        if not (row.get("data") or {}).get("workspace_token"):
            continue
        url = (row.get("url") or row.get("domain") or "").strip()
        if not url or url.split("//")[-1].split("/")[0].endswith(".example"):
            continue
        out.append(url if url.startswith("http") else f"https://{url}")
    return out
