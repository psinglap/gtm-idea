"""Email a short report after every scheduled run.

One line of intent: after each batch you should know what went out without opening anything.

A report is sent even when a run delivers nothing, and that is the important case. If mail only
arrived on success, then silence would mean either "the queue is empty" or "the whole thing has
been broken since Tuesday", and those look identical from an inbox. A crash stopped every send
for a full day and was noticed only because someone asked. So the run that sends zero still
reports, and says why zero.
"""
from __future__ import annotations

from typing import Optional


def build(report: dict, queue: dict) -> tuple:
    """(subject, body) for one pass."""
    delivered = (report.get("delivered") or {})
    enriched = (report.get("enriched") or {})
    judged = (report.get("judged") or {})
    sent = delivered.get("delivered") or 0
    skipped = delivered.get("skipped") or 0
    reasons = delivered.get("skip_reasons") or {}

    subject = f"Outreach: {sent} sent" if sent else "Outreach: 0 sent this run"

    lines = [
        f"Sent this run:  {sent}",
        f"Skipped:        {skipped}",
        f"Mode:           {delivered.get('mode', '?')}",
        f"Left today:     {delivered.get('remaining_today', '?')} of the daily cap",
        "",
        "This run",
        f"  Apollo lookups     {enriched.get('attempted', 0)}"
        f"   (+{enriched.get('reused', 0)} already known, no credit spent)",
        f"  emails found       {enriched.get('enriched', 0)}",
        f"  judged             {judged.get('judged', 0)}"
        f"   -> {judged.get('targets', 0)} target, {judged.get('rejected', 0)} not a fit",
        f"  bounces retired    {report.get('bounced', 0)}",
    ]

    if reasons:
        lines += ["", "Held back"]
        lines += [f"  {n:4}  {why}" for why, n in
                  sorted(reasons.items(), key=lambda kv: -kv[1])]

    lines += [
        "",
        "Queue",
        f"  waiting for Apollo   {queue.get('queued', 0)}",
        f"  ready to send        {queue.get('ready', 0)}",
        f"  sent, all time       {queue.get('sent', 0)}",
        f"  rejected by the ICP  {queue.get('rejected', 0)}",
    ]

    # Events, because the whole thing starts there. A report about emails alone cannot show the
    # top of the funnel drying up, which is the failure that takes longest to notice.
    ev = queue.get("events") or {}
    if ev:
        lines += [
            "",
            "Events coming up",
            f"  approved, you're in  {ev.get('approved', 0)}",
            f"  waiting on the host  {ev.get('pending_approval', 0)}",
            f"  invited, not yet in  {ev.get('invited', 0)}",
            f"  registered today     {ev.get('registered_today', 0)}",
            f"  guest lists read today {ev.get('scanned_today', 0)}",
        ]

    if not sent:
        # Name the difference between "nothing to do" and "something is wrong", because the
        # inbox cannot tell them apart and this is the whole reason a zero run still reports.
        lines += ["", "Nothing went out this run because:"]
        if queue.get("ready", 0) == 0 and queue.get("queued", 0) > 0:
            lines.append("  nobody is ready yet — Apollo and the judge are still working through")
            lines.append(f"  the {queue.get('queued', 0)} waiting. This is normal early on.")
        elif queue.get("queued", 0) == 0 and queue.get("ready", 0) == 0:
            lines.append("  the queue is empty. New people arrive when guest lists are read,")
            lines.append("  which needs Chrome open with the extension connected.")
        else:
            lines.append(f"  {queue.get('ready', 0)} were ready but all were held back — see above.")

    return subject, "\n".join(lines)


def queue_counts(store, cid: str) -> dict:
    """Aggregates, not rows. This ran once per report and once per scheduler tick, each time
    pulling every contact — see storage.event_contact_counts for what that cost."""
    from collections import Counter
    from warmgraph.models import utcnow
    from warmgraph.outreach import ingest

    counts = store.event_contact_counts(cid)
    buckets = counts.get("buckets") or []
    c = Counter()
    for b in buckets:
        c[b["status"]] += b["n"]

    regs = store.get_event_registrations(cid, limit=5000)
    events = {e.id: e for e in store.get_raw_events(since_days=400, limit=3000)
              if e.source == ingest.LUMA}
    today = utcnow().date().isoformat()
    upcoming = Counter(r.approval_status or "none" for r in regs
                       if events.get(r.event_id) and not ingest.has_ended(events[r.event_id]))
    ev = {
        "approved": upcoming.get("approved", 0),
        "pending_approval": upcoming.get("pending_approval", 0),
        "invited": upcoming.get("invited", 0),
        "registered_today": sum(1 for r in regs
                                if r.registered_at and str(r.registered_at)[:10] == today),
        "scanned_today": sum(1 for r in regs
                             if r.scanned_at and str(r.scanned_at)[:10] == today),
    }
    return {
        "events": ev,
        "queued": c.get("queued", 0),
        "ready": sum(b["n"] for b in buckets
                     if b["status"] == "judged" and b["verdict"] == "target"),
        "sent": c.get("sent", 0),
        "rejected": c.get("rejected", 0),
    }


def email_run(store, cid: str, report: dict, to: Optional[str] = None) -> str:
    """Send the report. Returns "" on success, otherwise the reason it did not go.

    Never raises. A report that fails must not be able to affect the run it is describing, and a
    run that already happened cannot be undone by failing to announce it.
    """
    from warmgraph import connections
    from warmgraph.connections import google
    try:
        mailboxes = [c for c in connections.gmail_mailboxes(store, cid) if c.provider == "gmail"]
        if not mailboxes:
            return "no gmail mailbox connected"
        address = to or mailboxes[0].account_label
        if not address:
            return "connected mailbox has no address on it"
        subject, body = build(report, queue_counts(store, cid))
        google.send_message(google.access_token_for(mailboxes[0]), address, subject, body)
        return ""
    except Exception as e:
        return str(e)[:200]
