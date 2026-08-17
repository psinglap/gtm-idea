"""The hourly server pass: sweep dead leases, judge, enrich, deliver.

Runs on a cron and only ever touches rows that are actually ready, so it is harmless when the
queue is empty and safe to run far more often than anything arrives. It does NOT do the
browser-bound work (registering, scanning, reading LinkedIn) — the extension owns that, which
is why this can run with the laptop closed and still make progress on whatever the browser
already produced.

Hourly rather than daily on purpose: the send cap is per-hour, and mail that trickles out
across the day looks like a person working rather than a batch job firing.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from warmgraph.agents.activities.event_icp_judge import EventIcpJudgeInput
from warmgraph.agents.activities.outreach_enrich import OutreachEnrichInput
from warmgraph.agents.activities.outreach_send import OutreachSendInput
from warmgraph.agents.base import Agent
from warmgraph import connections
from warmgraph.dates import parse_datetime
from warmgraph.storage import mirror


def _int_env(name: str, default: int) -> int:
    import os
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


class OutreachDailyInput(BaseModel):
    url: str
    # Per BATCH, not per run. Judging is not a thing to ration: enrichment already decides how
    # many people enter, and a verdict costs a fraction of the Apollo lookup that preceded it.
    # Capping it separately only invents a middle state — enriched, unjudged, undeliverable — that
    # grows every run and shows as "ready to send: 0" with thousands of people behind it. The run
    # below repeats until nothing is left to judge.
    judge_batch: int = _int_env("WG_JUDGE_BATCH", 200)
    judge_rounds: int = _int_env("WG_JUDGE_ROUNDS", 20)   # a runaway guard, not a budget
    # Apollo is the one upstream stage that costs real money per row, so it keeps a ceiling — a
    # ceiling on SPEND, not a pacing device. It runs in batches up to this total, then stops.
    #
    # 1,000, not 2,000. At four runs a day 2,000 is 8,000 credits, which can drain a month's
    # allowance in a day — and did: enrichment hit "insufficient credits" mid-pass and the queue
    # stopped moving. 1,000 still comfortably outruns the 200 emails a day it has to feed, since
    # roughly one in seven lookups becomes a send.
    #
    # Judging has no equivalent ceiling: a verdict is cheap, and rationing it only stranded people
    # between "enriched" and "sendable".
    enrich_limit: int = _int_env("WG_ENRICH_LIMIT", 1000)
    enrich_batch: int = _int_env("WG_ENRICH_BATCH", 500)
    send_limit: int = 200
    mode: Optional[str] = None
    dry_run: bool = False


class OutreachDailyReport(BaseModel):
    subject_domain: str
    leases_released: int = 0
    bounced: int = 0        # addresses retired from this pass's mailbox scan
    judged: dict = {}
    enriched: dict = {}
    delivered: dict = {}


class OutreachDailyAgent(Agent):
    name = "outreach_daily"
    description = ("One server pass over the event-outreach queue: release dead browser leases, "
                   "judge profiled attendees, enrich targets via Apollo, deliver the template.")
    InputModel = OutreachDailyInput
    OutputModel = OutreachDailyReport

    def run(self, inp: OutreachDailyInput) -> OutreachDailyReport:
        store = self.ctx.store
        profile = self.ctx.get_or_build_profile(inp.url)
        report = OutreachDailyReport(subject_domain=profile.domain)
        if store is None:
            return report

        cid = mirror.client_id_for(store, profile.domain)
        # First: anything a dead browser was holding goes back on the queue. Without this a
        # laptop closed mid-run would strand those rows in `reading` permanently.
        report.leases_released = store.release_expired_leases(cid)

        # Before sending anything new, read yesterday's failures back out of the mailbox. A
        # bounce that nobody retires is not a one-off: the address stays live in the queue, and
        # the domain keeps taking the reputation hit. Five addresses reached "sent" and bounced
        # before this ran anywhere, and they were only noticed because someone read their inbox.
        report.bounced = _retire_bounced(store, cid)

        judge = self.ctx.agents.get("event_icp_judge")
        enrich = self.ctx.agents.get("outreach_enrich")
        send = self.ctx.agents.get("outreach_send")

        # Order matters: enrich, then judge, then deliver. Judging first meant a contact enriched
        # in this pass waited for the NEXT one to be judged and the one after to be sent, so a
        # twice-daily schedule took a day and a half to move one person end to end.
        if enrich:
            # Until the queue is empty, or Apollo stops answering. A round that enriches nobody
            # and matches nobody has run out of work; a round that errors has run out of Apollo,
            # and either way there is no point asking again this pass.
            totals = {"attempted": 0, "reused": 0, "enriched": 0,
                      "no_match": 0, "unverified_email": 0, "errors": 0}
            done = 0
            while done < inp.enrich_limit:
                batch = min(inp.enrich_batch, inp.enrich_limit - done)
                out = enrich.run(OutreachEnrichInput(
                    url=inp.url, limit=batch)).model_dump(mode="json")
                for k in totals:
                    totals[k] += out.get(k) or 0
                totals["subject_domain"] = out.get("subject_domain", "")
                moved = (out.get("attempted") or 0) + (out.get("reused") or 0)
                done += moved
                # A round that errors has run out of Apollo; a round that moves nobody has run out
                # of queue. Either way there is nothing to gain by asking again this pass.
                if out.get("errors") or not moved:
                    break
            report.enriched = totals
        if judge:
            # Judge everything enrichment produced, in batches, rather than a fixed slice of it.
            total = {"judged": 0, "targets": 0, "rejected": 0, "skipped_no_profile": 0}
            for _ in range(max(1, inp.judge_rounds)):
                out = judge.run(EventIcpJudgeInput(
                    url=inp.url, limit=inp.judge_batch)).model_dump(mode="json")
                for k in ("judged", "targets", "rejected"):
                    total[k] += out.get(k) or 0
                total["skipped_no_profile"] = out.get("skipped_no_profile") or 0
                total["subject_domain"] = out.get("subject_domain", "")
                # Stop when a round touches nobody at all. Checking `judged` alone would stop
                # while rows were still being skipped for want of a profile — and since the query
                # returns the same rows next time, looping on them would spin instead of
                # progressing. Either way they are stuck, but the counter should say so.
                if not ((out.get("judged") or 0) + (out.get("skipped_no_profile") or 0)):
                    break
            report.judged = total
        if send:
            report.delivered = send.run(OutreachSendInput(
                url=inp.url, limit=inp.send_limit, mode=inp.mode,
                dry_run=inp.dry_run)).model_dump(mode="json")
        return report


BOUNCE_CHECK_KEY = "outreach_bounce_checked_at"

# How far back to look for addresses we sent to, when deciding which address in a failure notice
# is ours. Bounces are usually instant, so this could be the last run — but a server that
# greylists and retries can take hours to give up, and those are exactly the addresses most worth
# retiring. Missing one means mailing a dead address forever; widening the window costs an indexed
# query returning a few hundred rows. The asymmetry decides it.
BOUNCE_KNOWN_HOURS = 24


def _retire_bounced(store, cid: str) -> int:
    """Mark hard-bounced addresses dead and never mail them again.

    Both halves are time-bounded. Gmail is asked only for notices that arrived since the last
    check, instead of the same hundred every run; and the set of "addresses that are ours" comes
    from what we SENT in the last day, instead of loading all 10,930 contacts to build it.

    Soft failures are deliberately ignored — a full mailbox is temporary, and burning the address
    for it loses a real contact permanently. Failures here are swallowed: a mailbox that cannot be
    read must not stop the pass from sending to everyone whose address is fine.
    """
    from datetime import timedelta
    from warmgraph.connections import google
    from warmgraph.entities import DoNotContact
    from warmgraph.models import utcnow
    from warmgraph.outreach import bounces

    if not google.read_history_enabled():
        return 0
    try:
        conns = [c for c in connections.gmail_mailboxes(store, cid)]
        if not conns:
            return 0

        now = utcnow()
        client = store.get_company_by_id(cid)
        last_checked = ((client.data or {}).get(BOUNCE_CHECK_KEY) or "") if client else ""
        since = parse_datetime(last_checked) or (now - timedelta(hours=BOUNCE_KNOWN_HOURS))

        notices = []
        for conn in conns:
            token = google.access_token_for(conn)
            notices.extend(google.recent_messages(token, bounces.SEARCH, limit=100, after=since))

        # Which address in a notice is ours. A notice quotes the daemon, the sender and sometimes
        # a postmaster, and retiring the wrong one is silent and permanent.
        known = store.sent_addresses_since(cid, now - timedelta(hours=BOUNCE_KNOWN_HOURS))
        verdicts = bounces.scan(notices, known=known)
        dead = [addr for addr, kind in verdicts.items() if kind == "hard"]

        # Record the checkpoint whether or not anything bounced — otherwise a quiet run leaves the
        # window open and the next one re-reads everything again.
        if client is not None:
            client.data = {**(client.data or {}), BOUNCE_CHECK_KEY: now.isoformat()}
            store.upsert_company(client)

        if not dead:
            return 0

        changed = []
        for c in store.contacts_by_email(cid, dead):
            if c.status != "bounced":
                c.status = "bounced"
                c.last_error = "hard bounce read from the mailbox"
                changed.append(c)
        if changed:
            store.update_event_contacts(changed)
        store.save_do_not_contact([
            DoNotContact(company_id=cid, value=a, kind="email", reason="hard bounce")
            for a in dead])
        return len(dead)
    except Exception:
        return 0

