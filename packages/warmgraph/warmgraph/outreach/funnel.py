"""The pipeline as a funnel that BALANCES.

A count that does not subtract from anything cannot show where people are lost, and where they are
lost is the only actionable thing on the page: "7,820 waiting for Apollo" beside "10 email found"
reads as a contradiction until you can see that one is upstream of the other.

Kept out of the API layer so the arithmetic can be tested against a plain list of contacts. The
first version of this lived in the endpoint, and its test needed a database, a workspace and a
company id to run — it silently produced one row and the balance check passed on all zeros,
proving nothing.
"""
from __future__ import annotations

from collections import Counter
from typing import List


def stages(counts: dict) -> list:
    """Each stage: `on` continued, `out` stopped here and why. on + sum(out) == the stage above.

    Takes AGGREGATES, not rows. It used to take every contact and count them in Python, which
    meant reading the whole table on every render — and the UI re-rendered this every 30 seconds
    per open tab. That, more than anything else, is what exhausted the database's transfer quota
    and took the service down with it.
    """
    buckets = counts.get("buckets") or []

    def n(**want) -> int:
        total = 0
        for b in buckets:
            if all(b.get(k) == v for k, v in want.items()):
                total += b["n"]
        return total

    def where(pred) -> int:
        return sum(b["n"] for b in buckets if pred(b))

    total = sum(b["n"] for b in buckets)
    no_linkedin = n(status="no_linkedin")
    rest = total - no_linkedin
    waiting = n(status="queued")
    answered = rest - waiting

    done = lambda b: b["status"] not in ("no_linkedin", "queued")
    emailed = where(lambda b: done(b) and b["has_email"])
    no_email = answered - emailed
    judged = where(lambda b: done(b) and b["has_email"] and b["verdict"])
    unjudged = emailed - judged
    fit = where(lambda b: done(b) and b["has_email"] and b["verdict"] == "target")
    not_fit = judged - fit

    delivered = n(status="sent", verdict="target", has_email=True)
    bounced = n(status="bounced", verdict="target", has_email=True)
    ready = where(lambda b: b["verdict"] == "target" and b["has_email"]
                  and b["status"] in ("judged", "enriched"))
    held_why = counts.get("held") or {}

    return [
        {"label": "People from guest lists", "on": total, "out": [], "queue": []},
        {"label": "Have a LinkedIn URL", "on": rest, "queue": [],
         "out": [{"n": no_linkedin, "why": "no LinkedIn on the guest list — cannot look up"}]},
        {"label": "Looked up in Apollo", "on": answered, "out": [],
         "queue": [{"n": waiting, "why": "waiting for Apollo",
                    "next": "the next run looks up 2,000 of these"}]},
        {"label": "Have a usable email", "on": emailed, "queue": [],
         "out": [{"n": no_email, "why": "no address, or one Apollo would not vouch for"}]},
        {"label": "Judged against your ICP", "on": judged, "queue": [],
         "out": [{"n": unjudged, "why": "emailed before judging was required — historical"}]},
        {"label": "A fit", "on": fit, "queue": [],
         # "Wrong role for your ICP" is a tautology — WHICH roles is the part worth arguing with.
         "out": [{"n": not_fit, "why": "wrong role for your ICP",
                  "examples": [f"{c} x {t}" for t, c in (counts.get("reject_titles") or [])]}]},
        {"label": "Delivered", "on": delivered,
         "out": ([{"n": c, "why": f"held back — {w}"} for w, c in
                  sorted(held_why.items(), key=lambda kv: -kv[1])]
                 + [{"n": bounced, "why": "bounced, address retired"}]),
         "queue": [{"n": ready, "why": "a fit, verified, not yet written to",
                    "next": "goes out at the next run, up to 50"}]},
    ]


def event_stages(events: list, regs: dict, now, horizon_days: int, window_days: int,
                 keep: dict, stored_by_event: dict) -> tuple:
    """The same shape, for EVENTS: everything Luma showed us, down to the guest lists we read.

    It ends where the people funnel begins — "read their guest list" produces the people that
    funnel starts from — so the two together are one story rather than two unrelated panels.

    Returns (upcoming, past) as two separate funnels. They were one column, which made "past
    events you attended" read as 386% of the approved-upcoming line above it — a percentage
    between two numbers that have nothing to do with each other. They are different questions:
    what am I going to, and did the events I attended actually yield anyone.

    `keep` maps title -> whether the leisure filter kept it; `stored_by_event` maps event id ->
    how many contacts we actually hold. Both are passed in because the caller already has them.
    """
    from warmgraph.outreach import ingest

    seen = [e for e in events if e.source == ingest.LUMA]
    upcoming = [e for e in seen if not ingest.has_ended(e, now)]
    past = [e for e in seen if ingest.has_ended(e, now)]

    def reg(e):
        return regs.get(e.id)

    def status(e):
        r = reg(e)
        return r.approval_status if r else ""

    # --- what we could still act on -------------------------------------------------------
    # "Beyond the window" is a WAIT, not a rejection. It was listed beside paid and sold out,
    # which reads as "we will never do these" — and it is the opposite: those events enter the
    # queue on their own as their dates come up, and nobody has to remember them.
    paid, sold, leisure, far, actionable, already = [], [], [], [], [], []
    for e in upcoming:
        raw = e.raw or {}
        registered = status(e) in ("approved", "pending_approval", "waitlist")
        if not registered and (not raw.get("is_free", True) or raw.get("price")):
            paid.append(e)
        elif not registered and raw.get("is_sold_out"):
            sold.append(e)
        elif not registered and not keep.get(e.title, True):
            leisure.append(e)
        elif not ingest.starts_within(e, horizon_days):
            far.append(e)                 # including ones we are already in: they simply wait
        elif registered:
            already.append(e)
        else:
            actionable.append(e)

    worth = len(far) + len(already) + len(actionable)
    in_window = len(already) + len(actionable)
    invited_waiting = [e for e in far if status(e) == "invited"]

    approved_up = [e for e in already if status(e) == "approved"]
    ends = sorted(x for x in (ingest.parse_datetime(str((e.raw or {}).get("end_at") or ""))
                              or e.starts_at for e in approved_up) if x)
    next_end = ends[0] if ends else None
    pending_up = [e for e in already if status(e) == "pending_approval"]
    waitlist_up = [e for e in already if status(e) == "waitlist"]

    # --- and what the past ones yielded ---------------------------------------------------
    past_appr = [e for e in past if status(e) == "approved"]
    visible = [e for e in past_appr if reg(e) and reg(e).scannable]
    hidden = [e for e in past_appr if not (reg(e) and reg(e).scannable)]
    read = [e for e in visible if reg(e).scanned_at]
    unread = [e for e in visible if not reg(e).scanned_at]
    guests = sum((reg(e).guest_count or 0) for e in read)
    stored = sum(stored_by_event.get(e.id, 0) for e in read)

    return [
        {"label": "Events on your Luma", "on": len(seen), "out": [], "queue": []},
        {"label": "Still to come", "on": len(upcoming), "queue": [],
         "out": [{"n": len(past), "why": "already happened — see the next funnel"}]},
        {"label": "Worth registering for", "on": worth, "queue": [], "out": [
            {"n": len(paid), "why": "paid — never registered automatically"},
            {"n": len(sold), "why": "sold out"},
            {"n": len(leisure), "why": "not a work event"},
         ]},
        {"label": f"Inside the {horizon_days}-day window", "on": in_window, "out": [],
         "queue": [{"n": len(far),
                    "why": f"start beyond the {horizon_days}-day window",
                    "next": ("they join the queue on their own as their dates come up"
                             + (f" — {len(invited_waiting)} of them are invitations"
                                if invited_waiting else ""))}]},
        {"label": "Already registered", "on": len(already), "out": [],
         "queue": [{"n": len(actionable), "why": "free, relevant, not registered yet",
                    "next": "the next browser pass registers these"}]},
        # Waitlisted has to appear here. Without it this stage lost two people to nowhere: 38
        # "you are going" minus 8 waiting equalled 30, and the line below read 28. A funnel with an
        # unexplained gap is worse than no funnel, because it is the arithmetic that makes it
        # trustworthy at all.
        # The last line of this funnel is not an end — these events become the NEXT funnel once
        # they have happened, and nothing said so. "Approved — you're in" read as a destination
        # when it is the supply line: their guest lists are unreadable until the event is over.
        {"label": "Approved — you're in", "on": len(approved_up), "queue": [],
         "note": (f"their guest lists can be read once each event has ended"
                  + (f" — the soonest is {next_end:%b %d}" if next_end else "")),
         "out": [{"n": len(pending_up), "why": "waiting on the host to approve"},
                 {"n": len(waitlist_up), "why": "waitlisted — a place may still open up"}]},

    ], [
        {"label": "Events already held", "on": len(past), "out": [], "queue": []},
        {"label": "You were approved for", "on": len(past_appr), "queue": [],
         "out": [{"n": len(past) - len(past_appr),
                  "why": "not registered, or never approved"}]},
        {"label": "Guest list visible", "on": len(visible), "queue": [],
         "out": [{"n": len(hidden), "why": "the host hides the guest list"}]},
        {"label": "Guest list read", "on": len(read), "queue": (
            [{"n": len(unread), "why": "not read yet",
              "next": "the next browser pass reads these"}] if unread else []), "out": []},
        # The two funnels have to MEET. Luma's own guest_count was reported as the people we
        # collected, and it is not: it was 11,553 against 10,930 rows actually stored, and that
        # 623 went unexplained between two panels that were supposed to be one story. Most of it
        # is one event imported from a spreadsheet rather than scanned; the rest is a handful per
        # event that the guest list does not give us enough to store.
        # `unit` because these two count PEOPLE and everything above them counts EVENTS. Drawn on
        # one scale, 11,553 against a funnel that starts at 300 rendered as a bar four screens
        # wide — the number was right and the picture was nonsense.
        {"label": "Names on those guest lists", "on": guests, "out": [], "queue": [],
         "unit": "people"},
        {"label": "People we could store", "on": stored, "queue": [], "unit": "people",
         "out": [{"n": guests - stored,
                  "why": "on the list but not usable — no name, or a partial import"}]},
    ]
