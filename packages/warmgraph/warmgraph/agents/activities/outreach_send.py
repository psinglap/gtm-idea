"""Outreach delivery — suppression, then the template, then Gmail.

`WG_OUTREACH_MODE` decides what "deliver" means: `draft` leaves the message in Gmail Drafts
(the testing mode, and the default), `send` sends it. Both paths write the same
`outreach_messages` row, so suppression and the caps behave identically either way and flipping
the switch changes nothing else.

Suppression is re-checked here, immediately before delivery, rather than trusting a decision
made when the row was queued. Hours pass between those two moments, and the failure it prevents
is emailing someone twice.

Pacing is not about cost. 200 messages leaving a personal Gmail inside a minute is a textbook
spam signal; the same 200 spread across a day look like a person working.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel

import warmgraph.connections as connections
from warmgraph.agents.base import Agent
from warmgraph.connections import google
from warmgraph.entities import EventContact, OutreachMessage, RawEvent, email_domain, norm_email
from warmgraph.outreach import template
from warmgraph.storage import mirror


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


TEMPLATE_KEY = "outreach_template"
ANSWERS_KEY = "registration_answers"
QUESTIONS_KEY = "last_run_questions"


def load_template(store, company_id: str) -> template.MessageTemplate:
    """The client's own email, stored on their `companies` row. Falls back to the default copy
    so a fresh workspace can preview immediately rather than erroring on an empty template."""
    client = store.get_company_by_id(company_id) if store else None
    return template.MessageTemplate.from_dict((client.data or {}).get(TEMPLATE_KEY) if client else None)


def load_answers(store, company_id: str) -> dict:
    """The registration answer bank, defaults filled in behind whatever the user has set."""
    from warmgraph.outreach import registration
    client = store.get_company_by_id(company_id) if store else None
    stored = (client.data or {}).get(ANSWERS_KEY) if client else {}
    return registration.merge_defaults(stored or {})


def save_answers(store, company_id: str, new_answers: dict) -> dict:
    """Merge in newly supplied answers. The bank only ever grows: answering a question once
    means the same question at the next event fills itself."""
    client = store.get_company_by_id(company_id)
    if client is None:
        return {}
    stored = {**((client.data or {}).get(ANSWERS_KEY) or {})}
    stored.update({k: v for k, v in (new_answers or {}).items() if k and v})
    client.data = {**(client.data or {}), ANSWERS_KEY: stored}
    store.upsert_company(client)
    return stored


WORKER_STATUS_KEY = "worker_status"


def save_worker_status(store, company_id: str, status: dict) -> dict:
    """Last-write-wins. This is a liveness display, not a ledger — the durable record of what
    the worker actually did is the queue itself."""
    client = store.get_company_by_id(company_id)
    if client is None:
        return {}
    from warmgraph.models import utcnow
    # A heartbeat tick omits `counts` entirely rather than sending zeroes — merge so the last
    # real pass's numbers survive between runs instead of flashing back to 0.
    prev = dict((client.data or {}).get(WORKER_STATUS_KEY) or {})
    row = {**(status or {}), "received_at": utcnow().isoformat()}
    if not (status or {}).get("counts") and prev.get("counts"):
        row["counts"] = prev["counts"]
    client.data = {**(client.data or {}), WORKER_STATUS_KEY: row}
    store.upsert_company(client)
    return row


WORKER_LOG_KEY = "outreach_worker_log"
WORKER_LOG_KEEP = 120


def append_worker_log(store, company_id: str, lines: list) -> dict:
    """Append activity lines from the browser worker, newest last.

    Deduped on the worker's own sequence number so a retried POST cannot double up, and so two
    lines stamped in the same millisecond keep their order. Capped — this is a window onto what
    is happening now, not an audit trail.
    """
    client = store.get_company_by_id(company_id)
    if client is None:
        return {"lines": []}
    kept = list((client.data or {}).get(WORKER_LOG_KEY) or [])
    seen = {int(r.get("seq") or 0) for r in kept}
    for line in lines or []:
        seq = int(line.get("seq") or 0)
        if seq and seq in seen:
            continue
        seen.add(seq)
        kept.append({"seq": seq, "at": line.get("at") or "", "text": str(line.get("text") or "")[:200]})
    kept.sort(key=lambda r: r.get("seq") or 0)
    kept = kept[-WORKER_LOG_KEEP:]
    client.data = {**(client.data or {}), WORKER_LOG_KEY: kept}
    store.upsert_company(client)
    return {"lines": kept}


def load_worker_log(store, company_id: str, limit: int = 60) -> dict:
    client = store.get_company_by_id(company_id)
    if client is None:
        return {"lines": []}
    return {"lines": list((client.data or {}).get(WORKER_LOG_KEY) or [])[-limit:]}


RUN_REQUEST_KEY = "outreach_run_request"


def request_run(store, company_id: str, source: str = "ui") -> dict:
    """Ask the browser half to run its pass at the next opportunity.

    The browser cannot be reached from here — it is Chrome on someone's desk, behind no address.
    So a run request is a counter the worker reads on its own cheap tick. A counter rather than a
    boolean because the worker must be able to tell "a new request" from "the one I already ran",
    without the server having to know whether any browser is listening.
    """
    client = store.get_company_by_id(company_id)
    if client is None:
        return {}
    from warmgraph.models import utcnow
    prev = ((client.data or {}).get(RUN_REQUEST_KEY) or {}).get("seq") or 0
    row = {"seq": int(prev) + 1, "at": utcnow().isoformat(), "source": source}
    client.data = {**(client.data or {}), RUN_REQUEST_KEY: row}
    store.upsert_company(client)
    return row


def pending_run(store, company_id: str) -> dict:
    client = store.get_company_by_id(company_id)
    if client is None:
        return {"seq": 0}
    return (client.data or {}).get(RUN_REQUEST_KEY) or {"seq": 0}


def load_worker_status(store, company_id: str) -> dict:
    client = store.get_company_by_id(company_id) if store else None
    return dict((client.data or {}).get(WORKER_STATUS_KEY) or {}) if client else {}


def load_open_questions(store, company_id: str) -> list:
    """Questions the last run could not answer. Deliberately a single overwritten list, not
    per-event state: an event we cannot register today is simply retried tomorrow."""
    client = store.get_company_by_id(company_id) if store else None
    return list((client.data or {}).get(QUESTIONS_KEY) or []) if client else []


def save_open_questions(store, company_id: str, questions: list) -> list:
    client = store.get_company_by_id(company_id)
    if client is None:
        return []
    client.data = {**(client.data or {}), QUESTIONS_KEY: questions or []}
    store.upsert_company(client)
    return questions or []


def save_template(store, company_id: str, tmpl: template.MessageTemplate):
    client = store.get_company_by_id(company_id)
    if client is None:
        return None
    client.data = {**(client.data or {}), TEMPLATE_KEY: tmpl.to_dict()}
    store.upsert_company(client)
    return tmpl


ACCEPTED_EMAIL_STATUS = ("verified",)


def unreachable_reason(contact, email: str = "", fields: Optional[dict] = None) -> str:
    """Why this ADDRESS can never be written to, or "".

    Only properties of the address itself — the parts knowable the moment Apollo answers, so they
    can be applied before a verdict is spent. Everything that depends on OUR history with the
    person (already contacted, a mailbox thread, a meeting, do-not-contact) stays at delivery,
    because it changes between runs.
    """
    addr = email or (contact.email or "")
    catchall = (bool(fields.get("email_domain_catchall")) if fields is not None
                else bool(getattr(contact, "email_catchall", False)))
    if catchall and not _env_flag("WG_SEND_TO_CATCHALL"):
        return "catchall_domain"
    if is_academic(addr):
        return "academic_domain"
    return ""


def is_academic(email: str) -> bool:
    """True for a university address — a student, not a buyer.

    Free AI events in SF draw a lot of students, and they arrive on the guest list looking exactly
    like everyone else: a real name, a real LinkedIn, an address Apollo verifies. The role judge
    cannot catch them either, because "student" is often not what their headline says.

    Matches an `edu` label anywhere (purdue.edu, a-star.edu.sg) and the `ac.<cc>` form used
    outside the US (ox.ac.uk, u-tokyo.ac.jp). The country-code requirement on `ac` is what keeps
    a company called ac.com from being read as a university.
    """
    labels = email_domain(email).split(".")
    if "edu" in labels:
        return True
    return any(a == "ac" and len(b) == 2 and b.isalpha()
               for a, b in zip(labels, labels[1:]))


def _env_flag(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in ("1", "true", "yes", "on")


def _retire_draft_row(store, drafted) -> None:
    """Mark the old drafted row superseded, so it is not picked up again next pass."""
    drafted.status = "superseded"
    store.save_outreach_messages([drafted])


def suppression_reason(store, company_id: str, contact: EventContact, own_domain: str,
                       mailbox_history=None, calendar=None) -> str:
    """"" when this person is safe to email, otherwise the reason we're skipping them.

    THE MAILBOX IS THE SOURCE OF TRUTH for "have I already contacted this person". It knows
    about every conversation you have ever had, including the ones that predate this system and
    the ones you typed yourself. Our own `outreach_messages` log is kept purely as a free local
    fast path (and to count against the daily cap) — it can only ever ADD a skip, never remove
    one, so it cannot mask a hit Gmail would have found.
    """
    email = norm_email(contact.email)
    if not email:
        return "no_email"
    if own_domain and email_domain(email) == own_domain.lower():
        return "own_domain"
    if store.has_contacted(company_id, email):
        return "already_contacted"
    if store.is_do_not_contact(company_id, email):
        return "do_not_contact"

    # Email quality is checked HERE, at delivery, not only at enrichment. Enrichment is one way a
    # contact acquires an address; a spreadsheet import is another, and rows that arrived that way
    # carry no status at all. Gating only at enrichment let 37 rows reach the ready-to-send pool
    # with no verification between them.
    status = (getattr(contact, "email_status", "") or "").strip().lower()
    if status not in ACCEPTED_EMAIL_STATUS:
        return f"unverified_email:{status or 'unknown'}"

    # A catch-all domain accepts mail for every address at the SMTP door, so Apollo cannot tell a
    # real mailbox from one that has never existed and reports both as "verified". Two of the five
    # bounces so far were verified addresses on catch-all domains, and one drew a reply from a
    # human saying no such person works there. Set WG_SEND_TO_CATCHALL=1 to accept the risk.
    # Same helper enrichment uses, so the two stages can never drift into disagreeing about who
    # is reachable. Still checked here because a contact can arrive by import, never having been
    # enriched at all.
    unreachable = unreachable_reason(contact, email)
    if unreachable:
        return unreachable
    # A meeting outranks a mail thread: it survives a relationship that happened over LinkedIn,
    # WhatsApp or in person, where the mailbox has nothing to find. Checked before the Gmail
    # search because it is a set lookup against one sweep, not a query per address.
    if calendar is not None and email in calendar:
        return "meeting_scheduled"

    if mailbox_history is not None:
        try:
            if mailbox_history(email):
                return "prior_conversation"
        except Exception:
            # Never treat "could not check" as "no history" — that is exactly how a warm
            # contact receives a cold intro. Skip the person and say why.
            return "history_check_failed"
    return ""


def event_age_days(event_at: Optional[datetime], now: Optional[datetime] = None) -> Optional[int]:
    if event_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    if event_at.tzinfo is None:
        event_at = event_at.replace(tzinfo=timezone.utc)
    return (now.date() - event_at.date()).days


class OutreachSendInput(BaseModel):
    url: str
    limit: int = 200
    event_id: Optional[str] = None
    mode: Optional[str] = None      # overrides WG_OUTREACH_MODE for one run
    dry_run: bool = False           # render everything, touch nothing


class RenderedPreview(BaseModel):
    to: str
    subject: str
    body: str


class OutreachSendReport(BaseModel):
    subject_domain: str
    mode: str = "draft"
    delivered: int = 0
    skipped: int = 0
    failed: int = 0
    remaining_today: int = 0
    # Broken out from `delivered` so "we sent 30" cannot hide "28 of those were yesterday's
    # drafts finally going out", which is a different thing to have happened.
    sent_existing_drafts: int = 0
    skip_reasons: Dict[str, int] = {}
    previews: List[RenderedPreview] = []     # dry-run only


class OutreachSendAgent(Agent):
    name = "outreach_send"
    description = ("Deliver the follow-up template to enriched event targets: suppression check, "
                   "fixed template, Gmail draft or send, daily + hourly caps.")
    InputModel = OutreachSendInput
    OutputModel = OutreachSendReport

    def run(self, inp: OutreachSendInput) -> OutreachSendReport:
        store = self.ctx.store
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        mode = (inp.mode or os.getenv("WG_OUTREACH_MODE", "draft")).strip().lower()
        mode = mode if mode in ("draft", "send") else "draft"
        report = OutreachSendReport(subject_domain=domain, mode=mode)
        if store is None:
            return report

        cid = mirror.client_id_for(store, domain)
        daily_cap = _int_env("WG_OUTREACH_DAILY_CAP", 200)
        hourly_cap = _int_env("WG_OUTREACH_HOURLY_CAP", 30)

        sent_today = store.count_outreach_messages(cid, since_minutes=1440)
        sent_hour = store.count_outreach_messages(cid, since_minutes=60)
        budget = min(daily_cap - sent_today, hourly_cap - sent_hour, inp.limit)
        report.remaining_today = max(0, daily_cap - sent_today)
        if budget <= 0:
            return report

        # "judged", not "enriched": having an address is not the same as being worth writing to.
        # Delivery used to select on "enriched", which is the status Apollo sets, so a contact
        # went out the moment an email was found and the ICP never saw them.
        pending = [c for c in store.get_event_contacts(cid, event_id=inp.event_id,
                                                       status="judged", limit=inp.limit)
                   if c.verdict == "target"]

        # Anything already drafted is stranded otherwise. A drafted contact's status is no longer
        # "enriched", so switching the mode to send would mail the next batch and quietly leave
        # everything drafted under the old mode sitting in the mailbox forever.
        stranded = []
        if mode == "send" and not inp.dry_run:
            stranded = store.get_event_contacts(cid, event_id=inp.event_id, status="drafted",
                                                limit=inp.limit)
        if not pending and not stranded:
            return report

        # Fetch only the events this batch actually references (usually one or two), rather
        # than pulling every event on every pass.
        events: Dict[str, Optional[RawEvent]] = {}
        tmpl = load_template(store, cid)
        token = ""

        # One token per connected mailbox for the whole pass. Only `gmail` sends; ALL of them
        # are searched, because the mailbox you send from is usually not the one your history
        # lives in.
        mailbox_tokens: List[tuple] = []
        if not inp.dry_run:
            for conn in connections.gmail_mailboxes(store, cid):
                try:
                    mailbox_tokens.append((conn.account_label or conn.provider,
                                           google.access_token_for(conn)))
                except google.GoogleAuthError as e:
                    # This one IS the mailbox: a token we cannot refresh cannot send.
                    connections.mark_error(store, cid, conn.provider, str(e))
                    report.skip_reasons["gmail_auth_failed"] = 1
                    return report
                if conn.provider == "gmail":
                    token = mailbox_tokens[-1][1]
            if not token:
                # Say so. This returned silently, so "no sending account" and "nobody to send to"
                # were the same empty report — and that is precisely how a mailbox knocked into
                # an error state by an unrelated feature went unnoticed.
                report.skip_reasons["no_sending_mailbox"] = 1
                return report

        # One sweep for the whole pass. A failure here must not stop the send: not knowing about
        # a meeting is a worse email, but not sending at all is a worse outcome, and the Gmail
        # history check still catches anyone who was actually corresponded with.
        calendar = None
        if mailbox_tokens and google.read_calendar_enabled():
            calendar = set()
            for _label, tok in mailbox_tokens:
                try:
                    calendar |= google.calendar_attendees(tok)
                except Exception:
                    # NOT mark_error. Marking the connection broken because an OPTIONAL extra
                    # failed took the whole mailbox out of gmail_mailboxes(), which filters on
                    # status == "connected" — so the next run found no sending account, returned
                    # zero without a word, and sending stopped completely. A calendar we cannot
                    # read is a slightly worse suppression list, not a broken mailbox.
                    report.skip_reasons["calendar_unavailable"] = 1
                    calendar = None

        history = None
        if mailbox_tokens and google.read_history_enabled():
            seen: Dict[str, bool] = {}      # one lookup per address per pass

            def history(email: str) -> bool:            # noqa: F811
                if email not in seen:
                    seen[email] = any(google.has_conversation_with(tok, email)
                                      for _label, tok in mailbox_tokens)
                return seen[email]

        # Send the existing drafts first, before any new mail — they are the oldest, and under a
        # shared cap the newest batch would otherwise keep pushing them back.
        # ONE read for the whole batch, keyed by contact. This called a helper per contact that
        # fetched up to 2,000 drafted messages and scanned them in Python — 28 contacts meant
        # 56,000 rows read to find 28 things, and it grows with the square of the batch.
        drafts_by_contact = store.drafted_messages_by_contact(cid) if stranded else {}

        for contact in stranded:
            if report.delivered >= budget:
                break
            drafted = drafts_by_contact.get(contact.id)
            if drafted is None:
                continue          # nothing to send; it will be re-rendered as a normal row later
            try:
                mid, thread_id = google.send_stored_draft(
                    token, drafted.gmail_draft_id, contact.email,
                    drafted.subject, drafted.body)
            except google.GoogleAuthError as e:
                if "404" in str(e) or "notFound" in str(e):
                    # The draft is no longer in the mailbox, which means it was sent by hand or
                    # deleted. Either way it must not be re-created: a duplicate cold email is a
                    # worse outcome than never following up, so this closes the row rather than
                    # returning it to the queue.
                    contact.status = "sent"
                    contact.last_error = "draft no longer in mailbox — treated as sent by hand"
                    store.update_event_contacts([contact])
                    report.sent_existing_drafts += 1
                    continue
                contact.last_error = str(e)[:300]
                store.update_event_contacts([contact])
                report.failed += 1
                continue
            # Deliberately NOT re-running suppression. These passed it when they were drafted,
            # and the prior conversation the mailbox would now find is our own draft — checking
            # again would skip every single one of them as already-contacted.
            store.save_outreach_messages([OutreachMessage(
                company_id=cid, event_id=contact.event_id, person_id=contact.person_id,
                event_contact_id=contact.id, email=contact.email, email_key=contact.email,
                subject="", body="", status="sent",
                gmail_message_id=mid, gmail_thread_id=thread_id,
                sent_at=datetime.now(timezone.utc))])
            _retire_draft_row(store, drafted)
            contact.status = "sent"
            contact.last_error = ""
            store.update_event_contacts([contact])
            report.delivered += 1
            report.sent_existing_drafts += 1

        for contact in pending:
            if report.delivered >= budget:
                break

            if contact.event_id not in events:
                events[contact.event_id] = store.get_raw_event(contact.event_id)
            event = events[contact.event_id]
            event_name = event.title if event else ""
            event_at = event.starts_at if event else None
            reg = store.get_event_registration(cid, contact.event_id) if event else None
            event_short = reg.short_name if reg else ""

            reason = suppression_reason(store, cid, contact, domain, mailbox_history=history,
                                        calendar=calendar)
            if not reason:
                age = event_age_days(event_at)
                if age is not None and age > template.MAX_EVENT_AGE_DAYS:
                    reason = "event_too_old"
                else:
                    gaps = template.missing_fields(contact.name, event_name, tmpl)
                    if gaps:
                        reason = "missing:" + ",".join(gaps)

            if reason:
                self._record_skip(store, cid, contact, reason)
                report.skipped += 1
                report.skip_reasons[reason] = report.skip_reasons.get(reason, 0) + 1
                continue

            subject, body, html_body = template.render(
                name=contact.name, event_name=event_name, tmpl=tmpl,
                event_short=event_short)

            if inp.dry_run:
                report.previews.append(RenderedPreview(to=contact.email, subject=subject,
                                                       body=body))
                report.delivered += 1
                continue

            try:
                status, provider_id, thread_id = google.deliver(
                    token, mode, contact.email, subject, body, html_body=html_body)
            except google.GoogleAuthError as e:
                # Gmail is down / the token died — stop the pass instead of failing every row.
                contact.last_error = str(e)[:300]
                store.update_event_contacts([contact])
                connections.mark_error(store, cid, "gmail", str(e))
                report.failed += 1
                break

            store.save_outreach_messages([OutreachMessage(
                company_id=cid, event_id=contact.event_id, person_id=contact.person_id,
                event_contact_id=contact.id, email=contact.email, email_key=contact.email,
                subject=subject, body=body, status=status,
                gmail_draft_id=provider_id if status == "drafted" else "",
                gmail_message_id=provider_id if status == "sent" else "",
                gmail_thread_id=thread_id,
                sent_at=datetime.now(timezone.utc) if status == "sent" else None)])
            contact.status = status
            contact.last_error = ""
            store.update_event_contacts([contact])
            report.delivered += 1

        return report

    @staticmethod
    def _record_skip(store, cid: str, contact: EventContact, reason: str) -> None:
        """A skip is an audit record, not contact — `has_contacted` ignores `skipped`, so a
        person skipped today for a transient reason stays reachable tomorrow."""
        store.save_outreach_messages([OutreachMessage(
            company_id=cid, event_id=contact.event_id, person_id=contact.person_id,
            event_contact_id=contact.id, email=contact.email, email_key=norm_email(contact.email),
            status="skipped", skip_reason=reason)])
        contact.status = "skipped"
        contact.last_error = reason
        store.update_event_contacts([contact])
