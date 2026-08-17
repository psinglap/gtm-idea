#!/usr/bin/env python3
"""Hourly server pass for the event-outreach pipeline (Render cron entrypoint).

Does the half of the pipeline that needs no browser: release dead leases, judge attendees whose
LinkedIn was read, enrich targets via Apollo, deliver the template. The browser-bound half
(registering, scanning, reading LinkedIn) belongs to the extension, which is why this runs fine
with the laptop closed and simply has less to do on days Chrome never opened.

Safe to run on an empty queue — every stage filters to rows that are actually ready.

    WG_OUTREACH_URL=https://example.com python scripts/outreach_cron.py
    python scripts/outreach_cron.py --url https://example.com --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from warmgraph.service import WarmgraphService


def main() -> int:
    parser = argparse.ArgumentParser(description="One event-outreach pass.")
    parser.add_argument("--url", default="",
                        help="Limit the pass to one client URL. Default: every workspace.")
    parser.add_argument("--mode", default=None, choices=["draft", "send"],
                        help="Override WG_OUTREACH_MODE for this run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render everything, touch nothing.")
    parser.add_argument("--force", action="store_true",
                        help="Run even in manual mode. A person asking has already decided.")
    args = parser.parse_args()

    # THE switch, honoured here because this is the scheduler.
    #
    # It used to be ignored: WG_SCHEDULER stopped the scheduler inside the API and this kept
    # firing regardless, so "manual mode" was two settings in two services and easy to half-do.
    # Set it once as a Railway project-level shared variable and both obey it.
    #
    # Exiting on boot costs a container start and nothing else, which is the right trade for not
    # having a second place to remember.
    from warmgraph.outreach import scheduler
    if not (args.force or args.dry_run or scheduler.enabled()):
        print(json.dumps({"skipped": "manual mode (WG_SCHEDULER=0) — "
                                     "use the UI button or pass --force"}))
        return 0

    service = WarmgraphService()

    # No URL to configure: the workspaces ARE the list. Anyone who set up event outreach in
    # the app has a client row with a workspace token, and the cron runs for each of them.
    urls = [args.url] if args.url else workspace_urls(service.store)
    if not urls:
        print("No workspaces set up yet — nothing to do.")
        return 0

    failures = 0
    for url in urls:
        # The API process runs the same loop on its own schedule. Both are kept on purpose — a
        # second way to fire this is worth having, and the one controlled from a dashboard is
        # worth having most — but they must not run the same batch minutes apart. Whichever gets
        # the lease does the work; the other says so and exits 0, because being beaten to it is a
        # normal outcome, not a failure.
        if not args.dry_run and not claim(service, url):
            print(json.dumps({"url": url, "skipped": "another trigger ran within the last "
                                                     f"{scheduler_gap()} minutes"}))
            continue

        # Ask the browser half to run too, so a scheduled pass means the WHOLE loop and not just
        # the server's share of it. Chrome cannot be called from here, so this leaves the same
        # counter the web button leaves and the extension collects it on its next tick — within
        # five minutes, or whenever that laptop next opens. Skipped on a dry run, which is meant
        # to touch nothing.
        if not args.dry_run:
            request_browser_run(service.store, url)

        # run_and_record, like every other entry point. Calling the agent directly meant a cron
        # pass left no history row and no report email — so a run that happened was
        # indistinguishable from one that never fired, which is the failure this whole log exists
        # to catch. A dry run stays direct, since it is meant to touch nothing.
        if args.dry_run:
            report = service.run_agent("outreach_daily", {
                "url": url, "mode": args.mode, "dry_run": True,
            })
        else:
            from warmgraph.outreach import scheduler
            report = scheduler.run_and_record(service, url, source="cron",
                                              mode=args.mode)

        if not args.dry_run:
            send_digest(service, url)
        # Single-line JSON per workspace so Render's log view stays greppable.
        print(json.dumps({"url": url, **report}, default=str))
        failures += (report.get("delivered") or {}).get("failed") or 0

    # Non-zero only on real failures — an empty queue is not an error.
    return 1 if failures else 0


DIGEST_HOUR_UTC = int(os.getenv("WG_DIGEST_HOUR_UTC", "23") or 23)


def scheduler_gap() -> int:
    from warmgraph.outreach import scheduler
    return scheduler.MIN_RUN_GAP_MINUTES


def claim(service, url: str) -> bool:
    """Take the shared run lease, so this and the in-process scheduler cannot both fire."""
    from urllib.parse import urlparse
    from warmgraph.outreach import scheduler
    from warmgraph.storage import mirror

    domain = (urlparse(url).netloc or url).lower().removeprefix("www.")
    try:
        return scheduler.try_claim_run(service.store, mirror.client_id_for(service.store, domain),
                                       "cron")
    except Exception:
        return True                       # a lease we cannot read must not stop the cron working


def send_digest(service, url: str) -> None:
    """Email one summary a day, at the first run on or after DIGEST_HOUR_UTC.

    Keyed on the calendar day rather than a timer, so a missed run does not skip the report and a
    catch-up run does not send two. The hour defaults to 23:00 UTC — 4pm Pacific, the end of the
    working day, when the number is worth reading.

    A failure here is printed and swallowed. The digest describes the work; it must never be able
    to stop it.
    """
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    from warmgraph.connections import google
    from warmgraph import connections
    from warmgraph.outreach import digest
    from warmgraph.storage import mirror

    now = datetime.now(timezone.utc)
    if now.hour < DIGEST_HOUR_UTC:
        return
    day = now.date().isoformat()
    domain = (urlparse(url).netloc or url).lower().removeprefix("www.")
    try:
        store = service.store
        cid = mirror.client_id_for(store, domain)
        if digest.already_sent_today(store, cid, day):
            return
        mailboxes = [c for c in connections.gmail_mailboxes(store, cid) if c.provider == "gmail"]
        if not mailboxes:
            return
        to = mailboxes[0].account_label or ""
        if not to:
            return
        subject, body = digest.build(store, cid)
        token = google.access_token_for(mailboxes[0])
        google.send_message(token, to, subject, body)
        digest.mark_sent(store, cid, day)
        print(json.dumps({"url": url, "digest_sent_to": to, "subject": subject}))
    except Exception as e:
        print(json.dumps({"url": url, "digest_failed": str(e)[:200]}))


def request_browser_run(store, url: str) -> None:
    """Queue steps 1 to 4 — sync, register, re-check approvals, guest lists — for the extension.

    Without this a scheduled run did the server's half only: Apollo, judging and sending. Those
    stages consume a queue that nothing was refilling on the same schedule, so the loop ran twice
    a day at one end and twice a day at whatever unrelated time the browser happened to wake at
    the other.
    """
    from urllib.parse import urlparse
    from warmgraph.agents.activities import outreach_send
    from warmgraph.storage import mirror

    # removeprefix, not lstrip: lstrip takes a SET of characters, so "wow.example.com" came back
    # as "ow.example.com". Harmless for example.com, wrong for any host starting with w or a dot.
    domain = (urlparse(url).netloc or url).lower().removeprefix("www.")
    try:
        outreach_send.request_run(store, mirror.client_id_for(store, domain), source="cron")
    except Exception as e:                    # never let this stop the server half from running
        print(json.dumps({"url": url, "browser_run_request_failed": str(e)[:200]}))


def workspace_urls(store) -> list:
    """Every client that has completed setup. A workspace token is the marker: it only exists
    once someone has paired the app.

    `raw_scan` yields the stored row, where `url`/`domain` are columns and `data` is the JSON
    blob holding the token. This used to look for the token at `row["data"]["data"]` and the
    domain at `row["data"]["domain"]` — both one level too deep — so it matched nothing, printed
    "No workspaces set up yet", and exited 0. The hourly cron was a silent no-op for a full day
    while the queue sat at 475 and every dashboard read healthy. Hence test_workspace_urls.
    """
    out = []
    for row in store.raw_scan("companies"):
        if not (row.get("data") or {}).get("workspace_token"):
            continue
        url = (row.get("url") or row.get("domain") or "").strip()
        if not url:
            continue
        # RFC 2606 reserves .example for documentation and testing, so it can never be a real
        # customer. Test runs have left rows behind in this database before; skipping them here
        # keeps one stray fixture from turning every hourly pass into 15 pointless agent runs.
        if url.split("//")[-1].split("/")[0].endswith(".example"):
            continue
        out.append(url if url.startswith("http") else f"https://{url}")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
