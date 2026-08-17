"""Outreach enrichment — turn a judged target into a real, mailable person.

Apollo People Enrichment, keyed on the LinkedIn URL we already hold, returns the work email,
title, headline, seniority, department and employer in one call — a credit per person MATCHED
(misses are free).

This stage runs BEFORE any LinkedIn read, which is the opposite of how it started. Scraping a
LinkedIn profile is the slowest and by far the riskiest thing this system does: throttled, on
the user's real account, and an account restriction stops the whole pipeline. Apollo answers
the same questions from the same key — the LinkedIn URL — over an API, for people it knows.
So Apollo goes first and LinkedIn becomes the fallback for the ones it misses. On a live batch
that was 7 of 10, cutting LinkedIn reads by about seventy percent.

Two deliberate strictnesses:

  • **Verified emails only.** Apollo returns guesses alongside verified addresses. Sending to a
    guess is the fastest way to burn a sending reputation on bounces, so anything not
    `verified` is dropped with a reason rather than mailed hopefully.
  • **The employer becomes a real `customers` row.** Company details are stored once and
    referenced by `event_contacts.customer_id`, never duplicated per attendee.

Note on the provider waterfall: `contacts/waterfall.py` answers "find unknown people AT a
company", which is a different question from "enrich this specific person". Rather than bend
`ContactProvider` out of shape, this agent calls the Apollo client directly — the waterfall is
left alone for the signal-driven contacts path it already serves.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

import warmgraph.connections as connections
from warmgraph.agents.activities.people import resolve_people
from warmgraph.agents.base import Agent
from warmgraph.connections import apollo
from warmgraph.agents.activities import outreach_send
from warmgraph.entities import EventContact, Person, Prospect, RawPerson, norm_email
from warmgraph.storage import mirror

# Apollo's own confidence on the address. Anything else is a guess dressed up as data.
ACCEPTED_EMAIL_STATUS = ("verified",)


def reuse_known_person(c: EventContact, known: Person) -> EventContact:
    """Fill a queued contact from a person we already resolved, instead of paying Apollo again.

    Its own function because it needs to be callable from a test. The first version was inline in
    the loop and read `known.company_name`, which Person does not have — company name lives on the
    customer row, keyed by domain. That raised AttributeError on the FIRST person we already knew,
    which is precisely the case the path exists for, so enrichment died on every run and took
    judging and sending down with it. The test written alongside it asserted the person row was
    findable and never executed this, so it passed while the pipeline was dead.
    """
    c.person_id = known.id
    c.email = known.email
    c.email_key = known.email
    c.email_status = known.email_status or ""
    c.email_catchall = bool((known.data or {}).get("email_domain_catchall", False))
    c.title = known.title or (known.titles[0] if known.titles else c.title)
    c.status = "enriched"
    c.last_error = ""
    return c


def company_domain_of(fields: dict) -> str:
    """Bare domain from whatever Apollo gave us (it returns either a domain or a full URL)."""
    raw = (fields.get("company_domain") or "").strip().lower()
    if not raw:
        return ""
    raw = raw.split("//")[-1].split("/")[0]
    return raw[4:] if raw.startswith("www.") else raw


def person_from_apollo(c: EventContact, fields: dict) -> Person:
    return Person(
        person=c.name or " ".join(x for x in [fields.get("first_name"), fields.get("last_name")] if x),
        title=fields.get("title", ""),
        company_domain=company_domain_of(fields),
        linkedin_url=c.linkedin_url,
        email=norm_email(fields.get("email", "")),
        email_status=fields.get("email_status", ""),
        email_confidence=1.0 if fields.get("email_status") in ACCEPTED_EMAIL_STATUS else 0.0,
        location=fields.get("location", ""),
        titles=[fields["title"]] if fields.get("title") else [],
        providers=["apollo"],
        where_active=["linkedin", "luma"],
        source_url=c.linkedin_url,
    )


def raw_from_apollo(c: EventContact, fields: dict) -> RawPerson:
    return RawPerson(
        source="apollo", person=c.name, title=fields.get("title", ""),
        company_hint=fields.get("company_name", ""), linkedin_url=c.linkedin_url,
        email=norm_email(fields.get("email", "")), location=fields.get("location", ""),
        where_active=["linkedin", "luma"], event_refs=[c.event_id], raw=fields)


class OutreachEnrichInput(BaseModel):
    url: str
    limit: int = 50             # bounds credit spend per pass
    event_id: Optional[str] = None


class OutreachEnrichReport(BaseModel):
    subject_domain: str
    attempted: int = 0      # Apollo calls actually made — the billable number
    reused: int = 0         # answered from `people`, no credit spent
    enriched: int = 0
    no_match: int = 0
    unverified_email: int = 0
    unreachable: int = 0        # verified address we may never write to
    errors: int = 0


class OutreachEnrichAgent(Agent):
    name = "outreach_enrich"
    description = ("Find work emails for judged event targets via Apollo (People Enrichment by "
                   "LinkedIn URL). Verified addresses only; targets only, to bound credits.")
    InputModel = OutreachEnrichInput
    OutputModel = OutreachEnrichReport

    def run(self, inp: OutreachEnrichInput) -> OutreachEnrichReport:
        store = self.ctx.store
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        report = OutreachEnrichReport(subject_domain=domain)
        if store is None:
            return report

        cid = mirror.client_id_for(store, domain)
        api_key = connections.secret_for(store, cid, "apollo")
        if not api_key:
            # Say so. This returned an all-zero report, which is indistinguishable from "nothing
            # to enrich" — and that is exactly how a pass with 7,818 people queued reported
            # attempted 0 and nobody noticed until the numbers were read closely.
            report.errors += 1
            connections.mark_error(store, cid, "apollo",
                                   "No Apollo API key stored — reconnect Apollo.")
            return report

        # APOLLO RUNS FIRST, BEFORE ANY LINKEDIN READ.
        #
        # The original order was: read 129 LinkedIn profiles (throttled, on the user's real
        # account, hours of it), judge them, then ask Apollo for an email. But Apollo already
        # holds title, headline, seniority, department, employment history and the work email —
        # keyed on the same LinkedIn URL. Everything the ICP judge needs, in one call per ten
        # people, with no account risk at all.
        #
        # So `queued` contacts are enriched here, judged from Apollo's data, and LinkedIn is
        # read ONLY for the ones Apollo cannot match. Measured on a live batch: 7 of 10 matched,
        # so LinkedIn reads drop by roughly seventy percent. Every profile not scraped is
        # account risk not taken.
        #
        # `judged`+target contacts are still picked up, for anything that came the other way.
        pending = [c for c in store.get_event_contacts(cid, event_id=inp.event_id,
                                                       status="queued", limit=inp.limit)]
        pending += [c for c in store.get_event_contacts(cid, event_id=inp.event_id,
                                                        status="judged", limit=inp.limit)
                    if c.verdict == "target" and not c.email]
        # `profiled` is the LinkedIn worker's output, and it can arrive WITHOUT an address.
        # Those rows were a dead end: judging refuses them because a verdict on someone we cannot
        # reach is a verdict nobody acts on, and enrichment never looked at this status — so 58
        # people with a perfectly good LinkedIn URL sat in "awaiting judgment" indefinitely,
        # waiting for a stage that was never going to run. A LinkedIn URL is exactly what Apollo
        # takes, so they belong here.
        pending += [c for c in store.get_event_contacts(cid, event_id=inp.event_id,
                                                        status="profiled", limit=inp.limit)
                    if not c.email and c.linkedin_url]
        if not pending:
            return report

        seen_this_pass: dict = {}

        # Apollo is asked about TEN people per request, not one.
        #
        # This loop called match_by_linkedin, the single-person endpoint, so 2,000 contacts meant
        # 2,000 sequential HTTP round trips — measured at roughly 5 seconds each, which is three
        # hours for one pass and the real reason the queue never drained. bulk_match_by_linkedin
        # has existed since the Apollo-first rewrite and was never wired in here.
        #
        # Same credit cost: Apollo charges per person matched, and misses are free. The only
        # difference is the number of requests.
        for group in [pending[i:i + apollo.BULK_MAX]
                      for i in range(0, len(pending), apollo.BULK_MAX)]:
            if report.errors:
                break
            _enrich_group(self, store, cid, api_key, group, seen_this_pass, report)

        return report

    @staticmethod
    def _store_person(store, cid: str, c: EventContact, fields: dict, email: str) -> None:
        """raw_people (bronze) → people (identity-resolved, global) → customers (their employer)
        → the event_contact row that ties it together for this client."""
        store.save_raw_people([raw_from_apollo(c, fields)])

        incoming = person_from_apollo(c, fields)
        cdom = incoming.company_domain

        # Fast path: the LinkedIn URL is an exact identity key, and the hard gate guarantees
        # every lead here has one. An indexed hit means one row read instead of pulling the
        # whole `people` table through fuzzy resolution for each person enriched.
        exact = store.get_person_by_linkedin(c.linkedin_url)
        if exact is not None:
            merged, _, changed = resolve_people([exact], [incoming])
            changed_set = set(changed)
            store.save_people([p for p in merged if p.id in changed_set])
            person_id = exact.id
        else:
            # No exact match: fall back to fuzzy resolution, scoped to the employer so the
            # candidate set stays small rather than being every person we know.
            existing = store.get_people(company_domain=cdom) if cdom else []
            merged, id_map, changed = resolve_people(existing, [incoming])
            changed_set = set(changed)
            store.save_people([p for p in merged if p.id in changed_set])
            person_id = id_map.get(incoming.id, incoming.id)

        customer_id = ""
        if cdom or fields.get("company_name"):
            prospect = store.upsert_customer(Prospect(
                domain=cdom, name=fields.get("company_name", ""),
                name_key=(fields.get("company_name", "") or "").strip().lower(),
                website=f"https://{cdom}" if cdom else "",
                industry=fields.get("company_industry", ""),
                location=fields.get("location", "")))
            customer_id = prospect.id

        c.person_id = person_id
        c.customer_id = customer_id
        c.email = email
        c.email_key = email
        # Carry Apollo's verdict on the ADDRESS onto the contact, not just the address itself.
        # Delivery re-checks these, because enrichment is not the only way a contact acquires an
        # email — a spreadsheet import is another — and a row that arrives that way must not be
        # trusted. Leaving them unset here made every freshly enriched contact look unverified at
        # send time and skipped 21 of 24 people Apollo had just confirmed.
        c.email_status = fields.get("email_status", "")
        c.email_catchall = bool(fields.get("email_domain_catchall"))
        c.title = fields.get("title", "")
        c.company_name = fields.get("company_name", "")
        c.status = "enriched"
        c.last_error = ""
        store.update_event_contacts([c])


def _enrich_group(agent, store, cid: str, api_key: str, group, seen_this_pass: dict,
                  report) -> None:
    """Enrich up to BULK_MAX contacts with ONE Apollo request.

    Anyone we already know is answered locally first and never reaches the request, so a group of
    ten where six are already resolved asks Apollo about four.
    """
    to_ask = []
    for c in group:
        key = (c.linkedin_url or "").strip().lower()
        known = seen_this_pass.get(key)
        if known is None and key:
            known = store.get_person_by_linkedin(key)
        if known is not None and (known.email or "").strip():
            seen_this_pass[key] = known
            reuse_known_person(c, known)
            store.update_event_contacts([c])
            report.reused += 1
            continue
        to_ask.append(c)

    if not to_ask:
        return

    try:
        people = apollo.bulk_match_by_linkedin(api_key, [c.linkedin_url for c in to_ask])
    except apollo.ApolloError as e:
        # Key revoked or out of credits: stop the pass rather than burning through the rest of the
        # queue with the same failure, and surface it on the chip.
        for c in to_ask:
            c.last_error = str(e)[:300]
        store.update_event_contacts(to_ask)
        connections.mark_error(store, cid, "apollo", str(e))
        report.errors += 1
        return

    for c, person in zip(to_ask, list(people) + [None] * (len(to_ask) - len(people))):
        report.attempted += 1
        if not person:
            # Apollo does not know them. THIS is when LinkedIn is worth the risk — back to
            # `queued` so the LinkedIn stage picks them up, rather than dropped entirely.
            c.status, c.last_error = "queued", "apollo: no match — needs LinkedIn read"
            store.update_event_contacts([c])
            report.no_match += 1
            continue

        fields = apollo.person_fields(person)
        email = norm_email(fields.get("email", ""))
        if not email or fields.get("email_status") not in ACCEPTED_EMAIL_STATUS:
            c.status = "skipped"
            c.last_error = f"apollo: email not verified ({fields.get('email_status') or 'none'})"
            store.update_event_contacts([c])
            report.unverified_email += 1
            continue

        # Unreachable is decided HERE, before a verdict is spent on them.
        #
        # These checks also run at delivery, and have to: a contact can arrive by import, and the
        # mailbox and do-not-contact rules can only be applied at send time. But a catch-all
        # domain and an .edu address are properties of the ADDRESS, known the moment Apollo
        # answers — and judging someone we can never write to costs an LLM call to reach a verdict
        # nobody will act on. 133 of 941 verdicts were spent that way.
        #
        # They keep a status and a reason, so a change of policy can requeue them rather than
        # having to look them up again.
        unreachable = outreach_send.unreachable_reason(c, email, fields)
        if unreachable:
            c.email, c.email_key = email, email
            c.email_status = fields.get("email_status", "")
            c.email_catchall = bool(fields.get("email_domain_catchall"))
            c.status, c.last_error = "skipped", unreachable
            store.update_event_contacts([c])
            report.unreachable += 1
            continue

        agent._store_person(store, cid, c, fields, email)
        key = (c.linkedin_url or "").strip().lower()
        if key:
            seen_this_pass[key] = store.get_person_by_linkedin(key)
        report.enriched += 1
