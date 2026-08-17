"""A once-a-day email saying what the loop actually did.

The system runs unattended twice an hour and sends mail on its own. Without a report the only
ways to know whether it was working were to open the dashboard or to notice bounce notices in
the inbox — which is how a crash that stopped every send for a day went unnoticed until someone
asked why no email had gone out.

So the digest reports the things that are wrong as prominently as the things that went right. A
run that sent nothing is the most important message this can carry, and it is exactly the one a
"here's what we sent!" summary would leave blank.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Tuple

DIGEST_KEY = "outreach_digest_last"


def _counts(store, cid: str) -> dict:
    """Aggregates. The digest pulled every contact row once a day purely to count statuses."""
    return store.event_contact_counts(cid)


def build(store, cid: str, hours: int = 24) -> Tuple[str, str]:
    """(subject, plain-text body) covering the last `hours`."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    # Ask for the window, do not read the history. This fetched every message ever sent and
    # filtered by date in Python — 600 rows today for an answer of 50, and 36,000 in six months at
    # 200 a day. ix_om_window (company_id, status, created_at) covers exactly this query.
    sent = store.sent_messages_since(cid, since, limit=200)
    counts = _counts(store, cid)
    buckets = counts.get("buckets") or []
    status = Counter()
    for b in buckets:
        status[b["status"]] += b["n"]

    bounced_recent = status.get("bounced", 0)
    queued = status.get("queued", 0)

    # Ready to go out, by the same rules delivery uses. Reported because "0 sent" means something
    # completely different depending on whether anything was waiting.
    ready = sum(b["n"] for b in buckets
                if b["status"] == "judged" and b["verdict"] == "target")

    subject = f"Outreach: {len(sent)} sent in the last {hours}h"
    if not sent:
        subject = f"Outreach: NOTHING SENT in the last {hours}h"

    lines = [
        f"Sent in the last {hours} hours: {len(sent)}",
        "",
        "Pipeline",
        f"  waiting for Apollo      {queued}",
        f"  ready to send           {ready}",
        f"  sent (all time)         {status.get('sent', 0)}",
        f"  rejected by the ICP     {status.get('rejected', 0)}",
        f"  skipped                 {status.get('skipped', 0)}",
        f"  bounced and retired     {bounced_recent}",
        f"  no LinkedIn handle      {status.get('no_linkedin', 0)}",
    ]

    if not sent:
        lines += [
            "",
            "NOTHING WAS SENT. Worth checking, in this order:",
            f"  - is anything ready?  {ready} contacts have a target verdict",
            "  - did the scheduled run fire at all?",
            "  - is Gmail still connected?",
        ]

    if sent:
        lines += ["", "Who it went to"]
        lines += [f"  {m.email}" for m in sorted(sent, key=lambda m: m.sent_at)[:60]]
        if len(sent) > 60:
            lines.append(f"  ... and {len(sent) - 60} more")

    # Why people were held back. This is the number that explains a small send, and without it a
    # quiet day looks like a broken pipeline rather than a working filter.
    held = counts.get("held") or {}
    if held:
        lines += ["", "Held back"]
        lines += [f"  {n:5}  {why}" for why, n in
                  sorted(held.items(), key=lambda kv: -kv[1])[:8]]

    return subject, "\n".join(lines)


def already_sent_today(store, cid: str, day: str) -> bool:
    client = store.get_company_by_id(cid)
    if client is None:
        return True                      # no workspace, nothing to report to
    return ((client.data or {}).get(DIGEST_KEY) or "") == day


def mark_sent(store, cid: str, day: str) -> None:
    client = store.get_company_by_id(cid)
    if client is None:
        return
    client.data = {**(client.data or {}), DIGEST_KEY: day}
    store.upsert_company(client)
